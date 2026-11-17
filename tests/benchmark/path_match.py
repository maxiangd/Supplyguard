"""标准路径与真实返回路径的逐条比对。

判定口径：
- 一条路径由「传导规则 + 节点序列」唯一确定，规则不同不算同一条。
- 精确命中：规则和节点序列完全一致。
- 包含命中：标准节点序列是返回路径的连续子序列，即返回路径更长但把标准链路走全了。
  允许包含是因为展开规则可能补出更长的链，只要关键传导没丢就算找到。
- 排序位置：标准路径第一次出现在返回列表里的名次，用于判断它能否进入提示词证据。

所有函数是纯函数，不依赖 Neo4j / Qdrant / LLM。
"""

from typing import Any

# 聊天链路默认把前 15 条路径放进提示词，用同样的门槛衡量「关键路径进没进证据」。
DEFAULT_TOP_K = 15


def _normalize_one(path: dict) -> tuple[str, tuple[str, ...]] | None:
    """把一条路径统一成 (规则, 节点 ID 元组)。

    兼容两种来源：
    - cases.json 里的标准路径：{"rule": ..., "node_ids": [...]}
    - 接口返回的路径：{"rule": ..., "nodes": [{"id": ...}, ...]}
    """
    rule = path.get("rule") or ""

    node_ids = path.get("node_ids")
    if node_ids is None:
        node_ids = [node.get("id", "") for node in path.get("nodes") or []]

    node_ids = tuple(node_id for node_id in node_ids if node_id)
    if not node_ids:
        return None
    return rule, node_ids


def normalize_paths(paths: list[dict] | None) -> list[tuple[str, tuple[str, ...]]]:
    """批量归一化，丢弃没有节点信息的残缺路径。"""
    normalized = []
    for path in paths or []:
        item = _normalize_one(path)
        if item is not None:
            normalized.append(item)
    return normalized


def _is_contiguous_subsequence(outer: tuple[str, ...], inner: tuple[str, ...]) -> bool:
    """inner 是否是 outer 的一段连续子序列。"""
    if not inner or len(inner) > len(outer):
        return False
    width = len(inner)
    return any(
        outer[start : start + width] == inner
        for start in range(len(outer) - width + 1)
    )


def _matches(expected: tuple[str, tuple[str, ...]], actual: tuple[str, tuple[str, ...]]) -> str | None:
    """返回匹配级别：exact / contained / None。"""
    expected_rule, expected_nodes = expected
    actual_rule, actual_nodes = actual

    # 传导规则是语义的一部分，规则不同直接判定为不同路径
    if expected_rule != actual_rule:
        return None
    if expected_nodes == actual_nodes:
        return "exact"
    if _is_contiguous_subsequence(actual_nodes, expected_nodes):
        return "contained"
    return None


def _node_set(paths: list[tuple[str, tuple[str, ...]]]) -> set[str]:
    """把路径集合摊平成节点集合，重复节点只算一次。"""
    nodes: set[str] = set()
    for _, node_ids in paths:
        nodes.update(node_ids)
    return nodes


def _edge_set(paths: list[tuple[str, tuple[str, ...]]]) -> set[tuple[str, str]]:
    """把路径集合摊平成边集合。

    路径是节点序列，相邻节点构成一条有向边。这里不把规则带进边，
    因为规则描述的是整条路径，同一条结构边不应因为路径规则不同被重复计数。
    """
    edges: set[tuple[str, str]] = set()
    for _, node_ids in paths:
        edges.update(zip(node_ids, node_ids[1:]))
    return edges


def _prf(matched: int, golden_total: int, actual_total: int) -> tuple[float, float, float]:
    """返回 (precision, recall, f1)。"""
    recall = matched / golden_total if golden_total else 0.0
    precision = matched / actual_total if actual_total else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall > 0
        else 0.0
    )
    return precision, recall, f1


def _describe(path: tuple[str, tuple[str, ...]]) -> dict:
    """把归一化路径还原成可写进报告的结构。"""
    rule, node_ids = path
    return {"rule": rule, "node_ids": list(node_ids)}


def match_paths(
    expected_paths: list[dict] | None,
    actual_paths: list[dict] | None,
    top_k: int = DEFAULT_TOP_K,
) -> dict[str, Any]:
    """比对一条用例的标准路径与返回路径。

    匹配采用两轮贪心，同一返回路径只归属一条标准路径，避免一条长路径
    同时“命中”多条标准路径把召回率算高：
    1. 先配精确命中；
    2. 剩下的标准路径再找包含命中。

    返回：
      golden_total      标准路径数
      golden_exact      精确命中的标准路径数
      golden_matched    命中（精确或包含）的标准路径数
      actual_total      返回路径数
      actual_matched    被标准路径认领的返回路径数（用于算精确率）
      ranks             每条标准路径第一次出现的名次（1 开始，未出现为 None）
      rank_hits         进入前 top_k 的标准路径数
      top_k             排序门槛
      mrr               标准路径的平均倒数名次
      node_precision / node_recall / node_f1
                        节点级指标，忽略顺序与相邻关系
      edge_precision / edge_recall / edge_f1
                        边级指标，能发现“节点都在但传导关系走错”
      missing_golden    没被找到的标准路径
      unmatched_actual  没有被任何标准路径认领的返回路径
    """
    golden = normalize_paths(expected_paths)
    actual = normalize_paths(actual_paths)

    claimed: set[int] = set()
    golden_exact = [False] * len(golden)
    golden_matched = [False] * len(golden)

    # 第一轮：精确命中优先，保证“标准链路原样返回”不会被更长的路径抢走
    for expected_index, expected in enumerate(golden):
        for actual_index, candidate in enumerate(actual):
            if actual_index in claimed:
                continue
            if _matches(expected, candidate) == "exact":
                claimed.add(actual_index)
                golden_exact[expected_index] = True
                golden_matched[expected_index] = True
                break

    # 第二轮：未精确命中的，再看是否被某条更长的返回路径包含
    for expected_index, expected in enumerate(golden):
        if golden_matched[expected_index]:
            continue
        for actual_index, candidate in enumerate(actual):
            if actual_index in claimed:
                continue
            if _matches(expected, candidate) == "contained":
                claimed.add(actual_index)
                golden_matched[expected_index] = True
                break

    # 排序位置不参与一对一分摊：每条标准路径独立找它在返回列表里最早的位置
    ranks: list[int | None] = []
    for expected in golden:
        rank = None
        for actual_index, candidate in enumerate(actual):
            if _matches(expected, candidate) is not None:
                rank = actual_index + 1
                break
        ranks.append(rank)

    golden_nodes = _node_set(golden)
    actual_nodes = _node_set(actual)
    matched_nodes = golden_nodes & actual_nodes
    node_precision, node_recall, node_f1 = _prf(
        len(matched_nodes), len(golden_nodes), len(actual_nodes)
    )

    golden_edges = _edge_set(golden)
    actual_edges = _edge_set(actual)
    matched_edges = golden_edges & actual_edges
    edge_precision, edge_recall, edge_f1 = _prf(
        len(matched_edges), len(golden_edges), len(actual_edges)
    )

    reciprocal_sum = sum(1 / rank for rank in ranks if rank)
    mrr = reciprocal_sum / len(golden) if golden else 0.0

    return {
        "golden_total": len(golden),
        "golden_exact": sum(golden_exact),
        "golden_matched": sum(golden_matched),
        "actual_total": len(actual),
        "actual_matched": len(claimed),
        "ranks": ranks,
        "rank_hits": sum(1 for rank in ranks if rank is not None and rank <= top_k),
        "top_k": top_k,
        "mrr": mrr,
        "node_precision": node_precision,
        "node_recall": node_recall,
        "node_f1": node_f1,
        "matched_node_total": len(matched_nodes),
        "golden_node_total": len(golden_nodes),
        "actual_node_total": len(actual_nodes),
        "edge_precision": edge_precision,
        "edge_recall": edge_recall,
        "edge_f1": edge_f1,
        "matched_edge_total": len(matched_edges),
        "golden_edge_total": len(golden_edges),
        "actual_edge_total": len(actual_edges),
        "missing_golden": [
            _describe(path)
            for index, path in enumerate(golden)
            if not golden_matched[index]
        ],
        "unmatched_actual": [
            _describe(path)
            for index, path in enumerate(actual)
            if index not in claimed
        ],
    }
