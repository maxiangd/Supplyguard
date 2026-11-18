"""Benchmark 指标计算：precision / recall / F1 / 路径命中率 / 越权拦截率。

所有计算纯函数，不依赖外部服务。
"""

import math
from typing import Any

from tests.benchmark.path_match import DEFAULT_TOP_K, match_paths


def _is_security_case(result: dict) -> bool:
    """跨角色文档越权用例只考察文档权限拦截，不参与实体/规则/路径统计。"""
    return result.get("scenario") == "document_leak"


def _leaked(result: dict) -> bool:
    """该用例是否把角色不可见的文档块交了出去。

    runner 对真实检索结果逐块核对 allowed_roles，leaked_document_ids
    记录具体泄漏了哪些文档。判据是“有没有返回不可见文档”，
    不是“返回结果是否为 0”。
    """
    return bool(result.get("document_role_leak")) or bool(
        result.get("leaked_document_ids")
    )


def wilson_interval(successes: int, total: int, z: float = 1.96) -> dict[str, float]:
    """小样本比例的 95% 置信区间。

    用例数少时，19/19 也会被误读成“稳定 100%”，区间能把这个不确定性写清楚。
    """
    if total <= 0:
        return {"low": 0.0, "high": 0.0}

    proportion = successes / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt((proportion * (1 - proportion) + z * z / (4 * total)) / total)
        / denominator
    )
    return {
        "low": round(max(0.0, center - margin), 4),
        "high": round(min(1.0, center + margin), 4),
    }


def _aggregate_path_comparison(
    results: list[dict], top_k: int = DEFAULT_TOP_K
) -> dict[str, Any]:
    """汇总标准路径比对结果。

    只统计带有 expected_paths 的非越权用例。没有标准路径的用例不进分母，
    避免把“本来就没有标准答案”的样本算成 0 分。
    """
    golden_total = 0
    golden_exact = 0
    golden_matched = 0
    actual_total = 0
    actual_matched = 0
    rank_hits = 0
    reciprocal_sum = 0.0
    matched_node_total = 0
    golden_node_total = 0
    actual_node_total = 0
    matched_edge_total = 0
    golden_edge_total = 0
    actual_edge_total = 0
    missing_total = 0
    unmatched_total = 0
    hit_cases = 0
    compared_cases = 0

    for result in results:
        if _is_security_case(result):
            continue
        expected_paths = result.get("expected_paths") or []
        if not expected_paths:
            continue
        comparison = match_paths(
            expected_paths, result.get("actual_path_details"), top_k=top_k
        )
        compared_cases += 1
        golden_total += comparison["golden_total"]
        golden_exact += comparison["golden_exact"]
        golden_matched += comparison["golden_matched"]
        actual_total += comparison["actual_total"]
        actual_matched += comparison["actual_matched"]
        rank_hits += comparison["rank_hits"]
        reciprocal_sum += comparison["mrr"] * comparison["golden_total"]
        matched_node_total += comparison["matched_node_total"]
        golden_node_total += comparison["golden_node_total"]
        actual_node_total += comparison["actual_node_total"]
        matched_edge_total += comparison["matched_edge_total"]
        golden_edge_total += comparison["golden_edge_total"]
        actual_edge_total += comparison["actual_edge_total"]
        missing_total += len(comparison["missing_golden"])
        unmatched_total += len(comparison["unmatched_actual"])
        if any(rank is not None and rank <= top_k for rank in comparison["ranks"]):
            hit_cases += 1

    node_precision, node_recall, node_f1 = _ratio_metrics(
        matched_node_total, golden_node_total, actual_node_total
    )
    edge_precision, edge_recall, edge_f1 = _ratio_metrics(
        matched_edge_total, golden_edge_total, actual_edge_total
    )

    return {
        "path_case_total": compared_cases,
        "path_golden_total": golden_total,
        "path_rank_k": top_k,
        "path_exact_rate": golden_exact / golden_total if golden_total else 0.0,
        "path_recall": golden_matched / golden_total if golden_total else 0.0,
        "path_precision": actual_matched / actual_total if actual_total else 0.0,
        "path_rank_hit_rate": rank_hits / golden_total if golden_total else 0.0,
        "path_mrr": reciprocal_sum / golden_total if golden_total else 0.0,
        "path_case_hit_rate": hit_cases / compared_cases if compared_cases else 0.0,
        "path_hit_ci": wilson_interval(hit_cases, compared_cases),
        "path_recall_ci": wilson_interval(golden_matched, golden_total),
        "node_precision": node_precision,
        "node_recall": node_recall,
        "node_f1": node_f1,
        "edge_precision": edge_precision,
        "edge_recall": edge_recall,
        "edge_f1": edge_f1,
        "path_missing_total": missing_total,
        "path_unmatched_total": unmatched_total,
    }


def _ratio_metrics(
    matched: int, golden_total: int, actual_total: int
) -> tuple[float, float, float]:
    """返回 (precision, recall, f1)，输入是跨用例累加的计数。"""
    precision = matched / actual_total if actual_total else 0.0
    recall = matched / golden_total if golden_total else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall > 0
        else 0.0
    )
    return precision, recall, f1


def compute_metrics(results: list[dict]) -> dict[str, Any]:
    """从 benchmark runner 的结果列表计算汇总指标。

    每条 result 来自 runner 的一次问答测评，包含：
      - expected_entities: list[str]   ground truth 期望命中的实体 ID
      - actual_entities: list[str]     真实链路返回的实体 ID
      - expected_min_paths: int        期望最少路径数
      - actual_paths: int              真实返回路径数
      - expected_paths: list[dict]     标准路径（规则 + 节点序列）
      - actual_path_details: list[dict] 真实返回路径（规则 + 节点序列）
      - expected_path_rules: list[str] 期望出现的传导规则
      - actual_path_rules: list[str]   真实出现的传导规则
      - document_role_leak: bool       是否返回了角色不可见的文档块
      - leaked_document_ids: list[str] 泄漏的具体文档 ID
      - document_hits: int             文档检索返回的块数
      - document_check_mode: str       api / dry_run
      - role: str                      提问角色
      - question: str
    """
    if not results:
        return {"error": "no results to compute"}

    total = len(results)
    document_leak_count = sum(1 for r in results if _leaked(r))

    # ---- 实体命中率 ----
    entity_precisions = []
    entity_recalls = []
    for r in results:
        if _is_security_case(r):
            continue  # 越权用例考察的是拦截，不是实体召回
        expected = set(r.get("expected_entities", []))
        actual = set(r.get("actual_entities", []))
        if not expected:
            continue  # held-out 用例不算实体指标
        tp = len(expected & actual)
        entity_precisions.append(tp / len(actual) if actual else 0.0)
        entity_recalls.append(tp / len(expected) if expected else 1.0)

    avg_entity_precision = (
        sum(entity_precisions) / len(entity_precisions) if entity_precisions else 0.0
    )
    avg_entity_recall = (
        sum(entity_recalls) / len(entity_recalls) if entity_recalls else 0.0
    )

    # ---- 路径命中率 ----
    path_hits = 0
    path_total = 0
    for r in results:
        if _is_security_case(r):
            continue
        expected_min = r.get("expected_min_paths", 0)
        actual = r.get("actual_paths", 0)
        if expected_min > 0:
            path_total += 1
            if actual >= expected_min:
                path_hits += 1

    path_hit_rate = path_hits / path_total if path_total > 0 else 0.0

    # ---- 路径质量 ----
    # 与标准路径逐条比对：召回看关键链路有没有丢，精确看返回里有多少噪声，
    # 排序命中看关键路径能不能进入提示词证据的前 K 条。
    path_quality = _aggregate_path_comparison(results)

    # ---- 规则命中率 ----
    rule_precisions = []
    rule_recalls = []
    for r in results:
        if _is_security_case(r):
            continue
        expected_rules = set(r.get("expected_path_rules", []))
        actual_rules = set(r.get("actual_path_rules", []))
        if not expected_rules:
            continue
        tp = len(expected_rules & actual_rules)
        rule_precisions.append(tp / len(actual_rules) if actual_rules else 0.0)
        rule_recalls.append(tp / len(expected_rules))

    avg_rule_precision = (
        sum(rule_precisions) / len(rule_precisions) if rule_precisions else 0.0
    )
    avg_rule_recall = (
        sum(rule_recalls) / len(rule_recalls) if rule_recalls else 0.0
    )

    # ---- 文档越权拦截率 ----
    # 通过标准：检索返回的每个文档块，其 allowed_roles 都包含提问角色。
    # 请求失败的用例单独计数，不进分母，避免把“没测成”当成拦截成功。
    leak_cases = [r for r in results if _is_security_case(r)]
    checked_cases = [
        r
        for r in leak_cases
        if r.get("document_check_mode") in ("api", "dry_run")
    ]
    error_cases = [
        r for r in leak_cases if r.get("document_check_mode") == "error"
    ]
    blocked_cases = [r for r in checked_cases if not _leaked(r)]
    document_leak_block_rate = (
        len(blocked_cases) / len(checked_cases) if checked_cases else 0.0
    )
    if any(r.get("document_check_mode") == "api" for r in checked_cases):
        document_check_mode = "api"
    elif error_cases:
        document_check_mode = "error"
    elif any(r.get("document_check_mode") == "baseline" for r in leak_cases):
        document_check_mode = "baseline"
    else:
        document_check_mode = "dry_run"
    document_hit_cases = sum(
        1 for r in leak_cases if r.get("document_hits", 0) > 0
    )

    # ---- 多轮追问 ----
    # 通过标准：继承到了上一轮实体，且路径/实体满足用例期望
    followup_cases = [r for r in results if r.get("scenario") == "multi_turn"]
    followup_passed = sum(
        1
        for r in followup_cases
        if r.get("inherited_entity_ids")
        and r.get("actual_paths", 0) >= r.get("expected_min_paths", 0)
        and set(r.get("expected_entities", [])) <= set(r.get("actual_entities", []))
        and not _leaked(r)
    )
    followup_success_rate = (
        followup_passed / len(followup_cases) if followup_cases else 1.0
    )

    # ---- 按角色分组 ----
    by_role: dict[str, dict] = {}
    for r in results:
        role = r.get("role", "unknown")
        if role not in by_role:
            by_role[role] = {"total": 0, "leaks": 0, "path_hits": 0, "path_total": 0}
        by_role[role]["total"] += 1
        if _leaked(r):
            by_role[role]["leaks"] += 1
        if r.get("expected_min_paths", 0) > 0:
            by_role[role]["path_total"] += 1
            if r.get("actual_paths", 0) >= r.get("expected_min_paths", 0):
                by_role[role]["path_hits"] += 1

    for role_data in by_role.values():
        role_data["path_hit_rate"] = (
            role_data["path_hits"] / role_data["path_total"]
            if role_data["path_total"] > 0
            else 0.0
        )

    # 按角色看路径质量：同一批用例在不同角色可见的图不同，指标必须拆开看
    role_path_quality = {
        role: _aggregate_path_comparison(
            [r for r in results if r.get("role", "unknown") == role]
        )
        for role in by_role
    }

    # 按场景拆分：单事件、双事件、多轮、边界用例的召回难度差别很大
    by_scenario: dict[str, list[dict]] = {}
    for r in results:
        by_scenario.setdefault(r.get("scenario") or "unknown", []).append(r)
    scenario_quality = {
        scenario: _aggregate_path_comparison(subset)
        for scenario, subset in by_scenario.items()
    }

    reviewed_cases = sum(
        1
        for r in results
        if (r.get("expected_paths") or []) and r.get("expected_paths_reviewed")
    )
    provisional_cases = sum(
        1
        for r in results
        if (r.get("expected_paths") or []) and not r.get("expected_paths_reviewed")
    )
    if provisional_cases and reviewed_cases:
        quality_scope = "mixed"
    elif reviewed_cases:
        quality_scope = "reviewed"
    else:
        quality_scope = "provisional"

    return {
        "total_cases": total,
        "entity_precision": round(avg_entity_precision, 4),
        "entity_recall": round(avg_entity_recall, 4),
        "entity_f1": round(
            2 * avg_entity_precision * avg_entity_recall / (avg_entity_precision + avg_entity_recall)
            if (avg_entity_precision + avg_entity_recall) > 0
            else 0.0,
            4,
        ),
        "path_hit_rate": round(path_hit_rate, 4),
        "path_case_total": path_quality["path_case_total"],
        "path_golden_total": path_quality["path_golden_total"],
        "path_rank_k": path_quality["path_rank_k"],
        "path_exact_rate": round(path_quality["path_exact_rate"], 4),
        "path_recall": round(path_quality["path_recall"], 4),
        "path_precision": round(path_quality["path_precision"], 4),
        "path_rank_hit_rate": round(path_quality["path_rank_hit_rate"], 4),
        "path_case_hit_rate": round(path_quality["path_case_hit_rate"], 4),
        "path_mrr": round(path_quality["path_mrr"], 4),
        "path_hit_ci": path_quality["path_hit_ci"],
        "path_recall_ci": path_quality["path_recall_ci"],
        "node_precision": round(path_quality["node_precision"], 4),
        "node_recall": round(path_quality["node_recall"], 4),
        "node_f1": round(path_quality["node_f1"], 4),
        "edge_precision": round(path_quality["edge_precision"], 4),
        "edge_recall": round(path_quality["edge_recall"], 4),
        "edge_f1": round(path_quality["edge_f1"], 4),
        "path_missing_total": path_quality["path_missing_total"],
        "path_unmatched_total": path_quality["path_unmatched_total"],
        "path_reviewed_case_total": reviewed_cases,
        "path_provisional_case_total": provisional_cases,
        "path_quality_scope": quality_scope,
        "rule_precision": round(avg_rule_precision, 4),
        "rule_recall": round(avg_rule_recall, 4),
        "document_leak_block_rate": round(document_leak_block_rate, 4),
        "document_leak_count": document_leak_count,
        "document_leak_case_total": len(leak_cases),
        "document_leak_case_checked": len(checked_cases),
        "document_leak_case_error": len(error_cases),
        "document_leak_case_blocked": len(blocked_cases),
        "document_leak_hit_cases": document_hit_cases,
        "document_leak_check_mode": document_check_mode,
        "followup_success_rate": round(followup_success_rate, 4),
        "followup_case_total": len(followup_cases),
        "followup_case_passed": followup_passed,
        "by_role": {
            role: {
                "total": d["total"],
                "leaks": d["leaks"],
                "path_total": d["path_total"],
                "path_hit_rate": round(d["path_hit_rate"], 4),
                "path_golden_total": role_path_quality[role]["path_golden_total"],
                "path_recall": round(role_path_quality[role]["path_recall"], 4),
                "path_precision": round(role_path_quality[role]["path_precision"], 4),
                "path_mrr": round(role_path_quality[role]["path_mrr"], 4),
                "edge_f1": round(role_path_quality[role]["edge_f1"], 4),
            }
            for role, d in sorted(by_role.items())
        },
        "by_scenario": {
            scenario: {
                "total": len(by_scenario[scenario]),
                "path_golden_total": quality["path_golden_total"],
                "path_recall": round(quality["path_recall"], 4),
                "path_precision": round(quality["path_precision"], 4),
                "path_rank_hit_rate": round(quality["path_rank_hit_rate"], 4),
                "path_mrr": round(quality["path_mrr"], 4),
                "edge_f1": round(quality["edge_f1"], 4),
            }
            for scenario, quality in sorted(scenario_quality.items())
        },
    }