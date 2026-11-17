"""图谱服务：按角色取子图、识别问题中的实体、计算多跳影响路径。"""

from app.core.configuration import settings
from app.db.graph_database import run_query
from app.services.entity_retrieval import ENTITY_ID_FIELD, search_entities

RULE_LABELS = {
    "DIRECT": "直接传导",
    "REGION": "地区传导",
    "PORT": "港口传导",
    "ROUTE": "航线传导",
    "SUPPLY": "供应传导",
}

SEVERITY_RANK = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}
RISK_TO_SEVERITY = {"high": "HIGH", "medium": "MEDIUM", "low": "LOW"}

# 同一问题命中多个实体时，事件优先级最高：事件本身就带传导规则
TYPE_PRIORITY = {"event": 0, "supplier": 1, "factory": 2, "port": 3, "route": 4, "region": 5}

# 问题意图词到传导规则的映射，用于证据精排
INTENT_RULE_HINTS = {
    "PORT": ("港口", "出海口", "出货", "运输", "物流", "舱位"),
    "ROUTE": ("航线", "绕行", "海运", "运费", "交付"),
    "REGION": ("地区", "产地", "矿", "资源", "合规", "法规"),
    "SUPPLY": ("供应", "采购", "原料", "供应商", "成本", "价格"),
    "DIRECT": ("召回", "停产", "事件", "影响", "质量"),
}

MAX_ENTITIES = 3
MAX_PATHS = 40
# 路径精排的 RRF 平滑项，沿用信息检索常用值
PATH_RRF_K = 60


def _visible(node: dict, role: str) -> bool:
    """管理员看全图，其他角色只能看到 allowed_roles 包含自己的节点。"""
    return role == "admin" or role in (node.get("allowed_roles") or [])


def _public(node: dict) -> dict:
    """对外只暴露展示字段，不返回别名和权限列表。"""
    return {
        "id": node["id"],
        "name": node["name"],
        "type": node["type"],
        "risk_level": node.get("risk_level"),
        "region": node.get("region"),
        "desc": node.get("desc"),
    }


def _load_nodes(role: str) -> list[dict]:
    """加载当前角色可见的全部节点。"""
    records = run_query(
        "MATCH (n:Entity) RETURN n AS node ORDER BY n.type, n.id"
    )
    nodes = [record["node"] for record in records]
    return [node for node in nodes if _visible(node, role)]


def overview(role: str) -> dict:
    """返回角色可见的节点与两端都可见的关系，供前端画图。"""
    nodes = _load_nodes(role)
    node_ids = {node["id"] for node in nodes}

    records = run_query(
        "MATCH (a:Entity)-[r]->(b:Entity) "
        "RETURN a.id AS source, b.id AS target, type(r) AS relation, properties(r) AS properties"
    )
    links = []
    for record in records:
        if record["source"] not in node_ids or record["target"] not in node_ids:
            continue
        properties = record["properties"] or {}
        detail = properties.get("material") or properties.get("reason") or ""
        links.append(
            {
                "source": record["source"],
                "target": record["target"],
                "relation": record["relation"],
                "detail": detail,
            }
        )

    return {"nodes": [_public(node) for node in nodes], "links": links}


def _keyword_candidates(question: str, role: str) -> list[dict]:
    """关键词/别名召回 Top N，按命中长度和类型排序。"""
    candidates = []
    for node in _load_nodes(role):
        terms = [node["name"], *(node.get("aliases") or [])]
        hits = [term for term in terms if term and term in question]
        if hits:
            candidates.append((max(len(term) for term in hits), node))

    candidates.sort(
        key=lambda item: (-item[0], TYPE_PRIORITY.get(item[1]["type"], 9))
    )
    return [node for _, node in candidates[: settings.entity_keyword_limit]]


def _nodes_by_ids(entity_ids: list[str], role: str) -> dict[str, dict]:
    """批量取节点并过滤不可见项，避免逐个查 Neo4j。"""
    if not entity_ids:
        return {}
    records = run_query(
        "MATCH (n:Entity) WHERE n.id IN $ids RETURN n AS node",
        {"ids": entity_ids},
    )
    return {
        record["node"]["id"]: record["node"]
        for record in records
        if _visible(record["node"], role)
    }


def _vector_candidates(question: str, role: str) -> list[dict]:
    """向量召回 Top N，按相似度顺序返回。

    索引故障或缺密钥时返回空，融合阶段自然退化为只用关键词。
    """
    try:
        if len(question.strip()) < settings.entity_vector_min_query_chars:
            return []
        hits = search_entities(question, role, limit=settings.entity_vector_limit)
    except Exception:
        return []

    ordered_ids: list[str] = []
    seen: set[str] = set()
    for hit in hits:
        entity_id = hit.get(ENTITY_ID_FIELD)
        if entity_id and entity_id not in seen:
            seen.add(entity_id)
            ordered_ids.append(entity_id)

    nodes = _nodes_by_ids(ordered_ids, role)
    return [nodes[entity_id] for entity_id in ordered_ids if entity_id in nodes]


def _rrf_fuse(
    keyword_nodes: list[dict], vector_nodes: list[dict], k: int
) -> list[tuple[dict, float]]:
    """RRF 融合：按名次累加，避免比较不可比的原始分数。"""
    scores: dict[str, float] = {}
    nodes: dict[str, dict] = {}

    for rank, node in enumerate(keyword_nodes, start=1):
        nodes[node["id"]] = node
        scores[node["id"]] = scores.get(node["id"], 0.0) + 1 / (k + rank)

    for rank, node in enumerate(vector_nodes, start=1):
        nodes[node["id"]] = node
        scores[node["id"]] = scores.get(node["id"], 0.0) + 1 / (k + rank)

    return [(nodes[entity_id], score) for entity_id, score in scores.items()]


def _select_entities(
    fused: list[tuple[dict, float]],
    keyword_ids: set[str],
    vector_nodes: list[dict],
) -> list[dict]:
    """规则消歧：事件优先，并丢弃被事件覆盖的泛化节点。

    三条规则：
    - 关键词命中事件：事件是权威结果，只保留关键词实体，完全忽略向量候选；
    - 关键词没命中事件、但向量第一名是事件：保留事件和全部关键词实体，
      向量只补因果起点，不覆盖已命中的地区/港口/供应商/工厂；
    - 没有可信事件：按 RRF 分数取前 3。
    """
    keyword_events = [
        node
        for node, _ in fused
        if node["type"] == "event" and node["id"] in keyword_ids
    ]

    if keyword_events:
        selected = [
            item
            for item in fused
            if item[0]["id"] in keyword_ids
            and item[0]["type"] not in ("region", "port", "route")
        ]
        selected.sort(
            key=lambda item: (
                0 if item[0]["type"] == "event" else 1,
                -item[1],
                TYPE_PRIORITY.get(item[0]["type"], 9),
                item[0]["id"],
            )
        )
        return [node for node, _ in selected[:MAX_ENTITIES]]

    vector_top = vector_nodes[0] if vector_nodes else None
    if vector_top and vector_top["type"] == "event":
        # 向量补出事件时，关键词已有的实体都要保留：事件只增加因果起点，
        # 不能反过来把已经确定命中的地区/港口挤掉，否则会整片丢失路径。
        selected = [
            (node, score)
            for node, score in fused
            if node["id"] == vector_top["id"] or node["id"] in keyword_ids
        ]
        selected.sort(
            key=lambda item: (
                0 if item[0]["type"] == "event" else 1,
                -item[1],
                TYPE_PRIORITY.get(item[0]["type"], 9),
                item[0]["id"],
            )
        )
        entities: list[dict] = []
        seen: set[str] = set()
        for node, _ in selected:
            if node["id"] not in seen:
                seen.add(node["id"])
                entities.append(node)
        return entities[:MAX_ENTITIES]

    fused.sort(
        key=lambda item: (
            -item[1],
            TYPE_PRIORITY.get(item[0]["type"], 9),
            item[0]["id"],
        )
    )
    return [node for node, _ in fused[:MAX_ENTITIES]]


def match_entities(question: str, role: str) -> list[dict]:
    """两路召回 + RRF 融合 + 规则消歧，返回最多 3 个种子实体。

    关键词和向量各取 Top N 候选，按名次融合；事件命中时优先事件并
    压制地区/港口/航线。向量不可用时自动退化为纯关键词召回。
    """
    keyword_nodes = _keyword_candidates(question, role)
    vector_nodes = _vector_candidates(question, role)
    fused = _rrf_fuse(keyword_nodes, vector_nodes, settings.entity_rrf_k)

    return _select_entities(
        fused,
        {node["id"] for node in keyword_nodes},
        vector_nodes,
    )


def _node(node_id: str) -> dict | None:
    """按 ID 取单个节点。"""
    records = run_query(
        "MATCH (n:Entity {id: $id}) RETURN n AS node", {"id": node_id}
    )
    return records[0]["node"] if records else None


def _supplier_factories(supplier_id: str, role: str) -> list[tuple[dict, dict]]:
    """供应商供货的工厂及其关系属性。"""
    records = run_query(
        "MATCH (s:Entity {id: $id})-[r:SUPPLIES]->(f:Entity) "
        "RETURN f AS node, properties(r) AS properties",
        {"id": supplier_id},
    )
    return [
        (record["node"], record["properties"] or {})
        for record in records
        if _visible(record["node"], role)
    ]


def _factory_ports(factory_id: str, role: str) -> list[dict]:
    """工厂的出海口。"""
    records = run_query(
        "MATCH (f:Entity {id: $id})-[:SHIPS_VIA]->(p:Entity) RETURN p AS node",
        {"id": factory_id},
    )
    return [record["node"] for record in records if _visible(record["node"], role)]


def _region_children(region_id: str, role: str) -> list[tuple[dict, str]]:
    """地区上的供应商（原料来源）与工厂（本地生产）。"""
    records = run_query(
        "MATCH (reg:Entity {id: $id})<-[r:SOURCED_FROM|LOCATED_IN]-(x:Entity) "
        "RETURN x AS node, type(r) AS relation",
        {"id": region_id},
    )
    return [
        (record["node"], record["relation"])
        for record in records
        if _visible(record["node"], role)
    ]


def _port_factories(port_id: str, role: str) -> list[dict]:
    """经该港口出货的工厂。"""
    records = run_query(
        "MATCH (p:Entity {id: $id})<-[:SHIPS_VIA]-(f:Entity) RETURN f AS node",
        {"id": port_id},
    )
    return [record["node"] for record in records if _visible(record["node"], role)]


def _route_ports(route_id: str, role: str) -> list[dict]:
    """航线连接的两端港口。"""
    records = run_query(
        "MATCH (rt:Entity {id: $id})-[:CONNECTS]->(p:Entity) RETURN p AS node",
        {"id": route_id},
    )
    return [record["node"] for record in records if _visible(record["node"], role)]


def _affecting_events(entity_id: str, role: str) -> list[dict]:
    """找出影响该实体的风险事件，用来给下游路径补上因果起点。"""
    records = run_query(
        "MATCH (e:Entity {type: 'event'})-[a:AFFECTS]->(n:Entity {id: $id}) "
        "RETURN e AS node, a.rule AS rule, a.severity AS severity, a.reason AS reason",
        {"id": entity_id},
    )
    return [
        record for record in records if _visible(record["node"], role)
    ]


def _build_path(
    rule: str,
    severity: str,
    reason: str,
    nodes: list[dict],
    relations: list[str],
) -> dict:
    """把节点序列和关系序列拼成前端可直接消费的路径。"""
    links = [
        {
            "source": nodes[index]["id"],
            "target": nodes[index + 1]["id"],
            "relation": relations[index],
        }
        for index in range(len(relations))
    ]
    return {
        "rule": rule,
        "rule_label": RULE_LABELS[rule],
        "severity": severity,
        "reason": reason,
        "hops": len(nodes) - 1,
        "summary": " → ".join(node["name"] for node in nodes),
        "nodes": [_public(node) for node in nodes],
        "links": links,
        "affected": nodes[-1]["id"],
    }


def _downstream_paths(
    node: dict,
    role: str,
    rule: str,
    severity: str,
    reason: str,
    prefix_nodes: list[dict],
    prefix_relations: list[str],
    max_hops: int,
) -> list[dict]:
    """沿着「供应商→工厂→港口」继续往下游走，对已受影响的节点补齐路径。"""
    paths = []
    if node["type"] == "supplier":
        for factory, _ in _supplier_factories(node["id"], role):
            nodes = [*prefix_nodes, factory]
            relations = [*prefix_relations, "SUPPLIES"]
            if len(nodes) - 1 > max_hops:
                continue
            paths.append(_build_path(rule, severity, reason, nodes, relations))
            for port in _factory_ports(factory["id"], role):
                if len(nodes) - 1 >= max_hops:
                    continue
                paths.append(
                    _build_path(
                        rule,
                        severity,
                        reason,
                        [*nodes, port],
                        [*relations, "SHIPS_VIA"],
                    )
                )
    elif node["type"] == "factory":
        for port in _factory_ports(node["id"], role):
            nodes = [*prefix_nodes, port]
            if len(nodes) - 1 > max_hops:
                continue
            paths.append(
                _build_path(
                    rule, severity, reason, nodes, [*prefix_relations, "SHIPS_VIA"]
                )
            )
    return paths


def _expand_entity(
    entity: dict, role: str, max_hops: int, rule: str, severity: str, reason: str
) -> list[dict]:
    """从一个实体出发，按类型向上下游展开路径。"""
    paths: list[dict] = []
    entity_type = entity["type"]

    if entity_type == "supplier":
        for factory, _ in _supplier_factories(entity["id"], role):
            paths.append(
                _build_path(
                    "SUPPLY", severity, reason, [entity, factory], ["SUPPLIES"]
                )
            )
            if max_hops >= 2:
                for port in _factory_ports(factory["id"], role):
                    paths.append(
                        _build_path(
                            "SUPPLY",
                            severity,
                            reason,
                            [entity, factory, port],
                            ["SUPPLIES", "SHIPS_VIA"],
                        )
                    )
    elif entity_type == "factory":
        for port in _factory_ports(entity["id"], role):
            paths.append(
                _build_path(
                    "SUPPLY", severity, reason, [entity, port], ["SHIPS_VIA"]
                )
            )
    elif entity_type == "region":
        for child, relation in _region_children(entity["id"], role):
            paths.append(
                _build_path(
                    "REGION", severity, reason, [entity, child], [relation]
                )
            )
            paths.extend(
                _downstream_paths(
                    child,
                    role,
                    "REGION",
                    severity,
                    reason,
                    [entity, child],
                    [relation],
                    max_hops,
                )
            )
    elif entity_type == "port":
        for factory in _port_factories(entity["id"], role):
            paths.append(
                _build_path(
                    "PORT", severity, reason, [entity, factory], ["SHIPS_VIA"]
                )
            )
    elif entity_type == "route":
        for port in _route_ports(entity["id"], role):
            paths.append(
                _build_path(
                    "ROUTE", severity, reason, [entity, port], ["CONNECTS"]
                )
            )
            for factory in _port_factories(port["id"], role):
                if max_hops >= 2:
                    paths.append(
                        _build_path(
                            "ROUTE",
                            severity,
                            reason,
                            [entity, port, factory],
                            ["CONNECTS", "SHIPS_VIA"],
                        )
                    )
    return paths


def _event_paths(event: dict, role: str, max_hops: int) -> list[dict]:
    """从事件出发，按 AFFECTS 上标注的规则做多跳展开。"""
    records = run_query(
        "MATCH (e:Entity {id: $id})-[a:AFFECTS]->(t:Entity) "
        "RETURN t AS node, a.rule AS rule, a.severity AS severity, a.reason AS reason",
        {"id": event["id"]},
    )

    paths = []
    for record in records:
        target = record["node"]
        if not _visible(target, role):
            continue
        rule = record["rule"]
        severity = record["severity"] or "MEDIUM"
        reason = record["reason"] or ""

        if rule == "DIRECT":
            paths.append(
                _build_path(
                    "DIRECT", severity, reason, [event, target], ["AFFECTS"]
                )
            )
            paths.extend(
                _downstream_paths(
                    target,
                    role,
                    "DIRECT",
                    severity,
                    reason,
                    [event, target],
                    ["AFFECTS"],
                    max_hops,
                )
            )
        elif rule == "REGION":
            for child, relation in _region_children(target["id"], role):
                paths.append(
                    _build_path(
                        "REGION",
                        severity,
                        reason,
                        [event, target, child],
                        ["AFFECTS", relation],
                    )
                )
                paths.extend(
                    _downstream_paths(
                        child,
                        role,
                        "REGION",
                        severity,
                        reason,
                        [event, target, child],
                        ["AFFECTS", relation],
                        max_hops,
                    )
                )
        elif rule == "PORT":
            for factory in _port_factories(target["id"], role):
                if max_hops >= 2:
                    paths.append(
                        _build_path(
                            "PORT",
                            severity,
                            reason,
                            [event, target, factory],
                            ["AFFECTS", "SHIPS_VIA"],
                        )
                    )
        elif rule == "ROUTE":
            for port in _route_ports(target["id"], role):
                if max_hops >= 2:
                    paths.append(
                        _build_path(
                            "ROUTE",
                            severity,
                            reason,
                            [event, target, port],
                            ["AFFECTS", "CONNECTS"],
                        )
                    )
                for factory in _port_factories(port["id"], role):
                    if max_hops >= 3:
                        paths.append(
                            _build_path(
                                "ROUTE",
                                severity,
                                reason,
                                [event, target, port, factory],
                                ["AFFECTS", "CONNECTS", "SHIPS_VIA"],
                            )
                        )
    return paths


def _evidence_score(path: dict, question: str) -> float:
    """路径与问题的匹配分，规则可解释，后续可换成 cross-encoder 或 LLM。"""
    score = SEVERITY_RANK.get(path["severity"], 0) - 0.5 * path["hops"]

    for hint in INTENT_RULE_HINTS.get(path["rule"], ()):
        if hint in question:
            score += 2
            break

    for node in path["nodes"]:
        name = node.get("name") or ""
        if len(name) >= 2 and name in question:
            score += 1.5
            break

    if any(node.get("type") == "event" for node in path["nodes"]):
        score += 0.5
    return score


def _intent_score(path: dict, question: str) -> int:
    """问题里命中了多少条该传导规则的意图词。"""
    return sum(
        1
        for hint in INTENT_RULE_HINTS.get(path["rule"], ())
        if hint in question
    )


def dedupe_paths(paths: list[dict]) -> list[dict]:
    """按「规则 + 节点序列」去重，保留先出现的那条。"""
    unique: dict[tuple, dict] = {}
    for path in paths:
        key = (path["rule"], tuple(node["id"] for node in path["nodes"]))
        unique.setdefault(key, path)
    return list(unique.values())


def rerank_paths(paths: list[dict], question: str) -> list[dict]:
    """证据精排：用 RRF 融合四路名次。

    四路信号分别是证据分（意图 + 字面覆盖）、严重度、跳数和规则意图命中。
    RRF 只看名次，不比较量纲不同的分数，避免严重度压过问题相关性。
    """
    if not paths:
        return []

    signals = [
        sorted(
            paths,
            key=lambda path: (
                -_evidence_score(path, question),
                path["hops"],
                path["rule"],
                path["summary"],
            ),
        ),
        sorted(
            paths,
            key=lambda path: (
                -SEVERITY_RANK.get(path["severity"], 0),
                path["hops"],
                path["rule"],
                path["summary"],
            ),
        ),
        sorted(
            paths,
            key=lambda path: (
                path["hops"],
                -SEVERITY_RANK.get(path["severity"], 0),
                path["rule"],
                path["summary"],
            ),
        ),
        sorted(
            paths,
            key=lambda path: (
                -_intent_score(path, question),
                path["hops"],
                -SEVERITY_RANK.get(path["severity"], 0),
                path["rule"],
                path["summary"],
            ),
        ),
    ]

    scores = {id(path): 0.0 for path in paths}
    for ordered in signals:
        for rank, path in enumerate(ordered, start=1):
            scores[id(path)] += 1 / (PATH_RRF_K + rank)

    return sorted(
        paths,
        key=lambda path: (
            -scores[id(path)],
            -_evidence_score(path, question),
            path["hops"],
            -SEVERITY_RANK.get(path["severity"], 0),
            path["rule"],
            path["summary"],
        ),
    )


def impact_paths(
    entities: list[dict], role: str, max_hops: int = 3, question: str = ""
) -> list[dict]:
    """对识别出的实体逐个展开，去重后按证据相关度排序。"""
    paths: list[dict] = []
    for entity in entities:
        if entity["type"] == "event":
            paths.extend(_event_paths(entity, role, max_hops))
            continue

        severity = RISK_TO_SEVERITY.get(entity.get("risk_level"), "MEDIUM")
        reason = f"{entity['name']}相关风险"
        base_paths = _expand_entity(entity, role, max_hops, "SUPPLY", severity, reason)
        events = _affecting_events(entity["id"], role)
        if not events:
            paths.extend(base_paths)
            continue

        # 实体本身没有因果上下文，用影响它的事件作为路径起点，避免回答缺少风险来源
        # 零跳起点让「事件 → 实体」也能成立，不依赖该角色是否看得到下游港口
        prefix_bases = [
            _build_path("SUPPLY", severity, reason, [entity], []),
            *base_paths,
        ]
        for record in events:
            event = record["node"]
            for path in prefix_bases:
                if path["hops"] + 1 > max_hops:
                    continue
                paths.append(
                    _build_path(
                        record["rule"],
                        record["severity"] or severity,
                        record["reason"] or "",
                        [event, *path["nodes"]],
                        ["AFFECTS", *[link["relation"] for link in path["links"]]],
                    )
                )

    # 每条路径的节点都来自上面带 _visible 过滤的查询，这里只需去重与精排
    visible_paths = dedupe_paths(paths)
    return rerank_paths(visible_paths, question)[:MAX_PATHS]


def impact(
    question: str | None,
    entity_id: str | None,
    role: str,
    max_hops: int = 3,
    extra_entity_ids: list[str] | None = None,
) -> dict:
    """问答与图谱页共用的入口：问题或实体名 → 命中实体与影响路径。

    extra_entity_ids 用于多轮追问：当前问题命中不到实体时，
    继承上一轮对话的实体继续展开，避免「那它呢？」变成空结果。
    """
    entities: list[dict] = []
    if question:
        entities = match_entities(question, role)
    elif entity_id:
        node = _node(entity_id)
        if node and _visible(node, role):
            entities = [node]

    # 当前问题没命中实体时，才使用追问继承来的实体；新问题优先
    if not entities and extra_entity_ids:
        seen: set[str] = set()
        for inherited_id in extra_entity_ids:
            if inherited_id in seen:
                continue
            seen.add(inherited_id)
            node = _node(inherited_id)
            if node and _visible(node, role):
                entities.append(node)
            if len(entities) >= MAX_ENTITIES:
                break

    paths = impact_paths(entities, role, max_hops, question or "")
    return {
        "entities": [_public(entity) for entity in entities],
        "paths": paths,
    }


def alternatives(entity_ids: list[str], entity_type: str, role: str) -> list[dict]:
    """找出不受影响、可替代的同类型节点。

    港口替代：查哪些港口没有被 AFFECTS 边指向且服务相同工厂。
    航线替代：查哪些航线没有被 AFFECTS 边指向且连接相同区域。
    """
    if entity_type == "port":
        return _port_alternatives(entity_ids, role)
    if entity_type == "route":
        return _route_alternatives(entity_ids, role)
    return []


def _port_alternatives(disrupted_ids: list[str], role: str) -> list[dict]:
    """排除受影响的港口，返回角色可见的其他港口。"""
    # 找出受影响港口服务的工厂
    factory_records = run_query(
        "MATCH (f:Entity {type: 'factory'})-[:SHIPS_VIA]->(p:Entity) "
        "WHERE p.id IN $ids RETURN DISTINCT f.id AS fid, f.name AS fname",
        {"ids": disrupted_ids},
    )
    factory_map = {r["fid"]: r["fname"] for r in factory_records}

    # 找有哪些港口被事件影响了（有 AFFECTS 入边的港口）
    affected_records = run_query(
        "MATCH (e:Entity {type: 'event'})-[a:AFFECTS]->(p:Entity {type: 'port'}) "
        "RETURN DISTINCT p.id AS pid"
    )
    affected_ids = {r["pid"] for r in affected_records} | set(disrupted_ids)

    # 查所有角色可见且未被影响的港口
    all_ports = _load_nodes(role)
    candidates = [p for p in all_ports if p["type"] == "port" and p["id"] not in affected_ids]

    result = []
    for port in candidates:
        # 查该港口服务哪些工厂（从上面 factory_map 的工厂里）
        serving = run_query(
            "MATCH (f:Entity)-[:SHIPS_VIA]->(p:Entity {id: $pid}) "
            "WHERE f.id IN $fids RETURN f.name AS name",
            {"pid": port["id"], "fids": list(factory_map.keys())},
        )
        serves = [r["name"] for r in serving]
        # 也查该港口服务的所有工厂，用于通用替代
        all_serving = run_query(
            "MATCH (f:Entity {type: 'factory'})-[:SHIPS_VIA]->(p:Entity {id: $pid}) "
            "RETURN f.name AS name",
            {"pid": port["id"]},
        )
        all_serves = [r["name"] for r in all_serving if _visible(r, role)]

        entry = _public(port)
        entry["serves"] = serves if serves else all_serves
        result.append(entry)

    # 按"能服务多少受影响工厂"排序
    result.sort(key=lambda p: len(p.get("serves", [])), reverse=True)
    return result


def _route_alternatives(disrupted_ids: list[str], role: str) -> list[dict]:
    """排除受影响的航线，返回角色可见的其他航线。"""
    affected_records = run_query(
        "MATCH (e:Entity {type: 'event'})-[a:AFFECTS]->(r:Entity {type: 'route'}) "
        "RETURN DISTINCT r.id AS rid"
    )
    affected_ids = {r["rid"] for r in affected_records} | set(disrupted_ids)

    all_nodes = _load_nodes(role)
    candidates = [n for n in all_nodes if n["type"] == "route" and n["id"] not in affected_ids]

    result = []
    for route in candidates:
        ports = run_query(
            "MATCH (rt:Entity {id: $rid})-[:CONNECTS]->(p:Entity) "
            "RETURN p.name AS name",
            {"rid": route["id"]},
        )
        entry = _public(route)
        entry["connects"] = [r["name"] for r in ports]
        result.append(entry)

    return result