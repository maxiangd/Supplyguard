"""路径质量评测：标准路径比对与指标聚合。

这组测试保护两件事：
1. 比对口径本身正确（精确、包含、规则区分、一对一分摊、排序位置）。
2. 用例里的标准路径与参考实现保持一致，防止以后改规则却忘了更新标准答案。
"""

import json
from pathlib import Path

import pytest

from tests.benchmark.ground_truth import expected_paths_for_case
from tests.benchmark.metrics import compute_metrics, wilson_interval
from tests.benchmark.path_match import match_paths

CASES_PATH = Path(__file__).parent / "benchmark" / "cases.json"


def load_cases() -> list[dict]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def path(rule: str, *node_ids: str) -> dict:
    """构造标准路径。"""
    return {"rule": rule, "node_ids": list(node_ids)}


def actual(rule: str, *node_ids: str) -> dict:
    """构造接口返回格式的路径。"""
    return {"rule": rule, "nodes": [{"id": node_id} for node_id in node_ids]}


def make_result(expected_paths, actual_paths, scenario="single_event") -> dict:
    """构造一条参与指标计算的评测结果。"""
    return {
        "scenario": scenario,
        "role": "admin",
        "expected_entities": [],
        "actual_entities": [],
        "expected_min_paths": 1,
        "actual_paths": len(actual_paths),
        "expected_path_rules": [],
        "actual_path_rules": [],
        "expected_paths": expected_paths,
        "actual_path_details": actual_paths,
        "document_role_leak": False,
        "leaked_document_ids": [],
    }


# ---------------------------------------------------------------------------
# 比对口径
# ---------------------------------------------------------------------------


def test_exact_match_ignores_payload_shape():
    """标准路径用 node_ids、接口返回用 nodes，归一化后应判为精确命中。"""
    comparison = match_paths(
        [path("REGION", "event", "region")],
        [actual("REGION", "event", "region")],
    )

    assert comparison["golden_total"] == 1
    assert comparison["golden_exact"] == 1
    assert comparison["golden_matched"] == 1
    assert comparison["ranks"] == [1]


def test_longer_actual_path_contains_golden_path():
    """返回路径更长但包含标准链路时，算包含命中，不算精确命中。"""
    comparison = match_paths(
        [path("REGION", "event", "region")],
        [actual("REGION", "event", "region", "factory")],
    )

    assert comparison["golden_exact"] == 0
    assert comparison["golden_matched"] == 1
    assert comparison["actual_matched"] == 1


def test_path_missing_middle_node_is_not_contained():
    """节点不相邻时不算包含，避免把不同传导链误判为同一条。"""
    comparison = match_paths(
        [path("REGION", "event", "region")],
        [actual("REGION", "event", "supplier", "factory")],
    )

    assert comparison["golden_matched"] == 0


def test_rule_must_match():
    """节点序列相同但传导规则不同，是两条不同的路径。"""
    comparison = match_paths(
        [path("REGION", "event", "region")],
        [actual("DIRECT", "event", "region")],
    )

    assert comparison["golden_matched"] == 0
    assert comparison["actual_matched"] == 0


def test_extra_actual_paths_lower_precision():
    """多返回一条无关路径不影响召回，但会拉低精确率。"""
    comparison = match_paths(
        [path("REGION", "event", "region")],
        [
            actual("REGION", "event", "region"),
            actual("PORT", "event", "port"),
        ],
    )

    assert comparison["golden_matched"] == 1
    assert comparison["actual_total"] == 2
    assert comparison["actual_matched"] == 1


def test_one_actual_path_only_claims_one_golden_path():
    """一条长路径即使包含多条标准路径，也只按贪心认领一条，避免召回率虚高。"""
    comparison = match_paths(
        [path("REGION", "event", "region"), path("REGION", "region", "factory")],
        [actual("REGION", "event", "region", "factory")],
    )

    assert comparison["golden_total"] == 2
    assert comparison["golden_matched"] == 1
    assert comparison["actual_matched"] == 1


def test_rank_uses_first_matching_position():
    """排序位置决定标准路径能否进入提示词证据范围。"""
    expected = [path("REGION", "event", "region")]
    returned = [
        actual("PORT", "event", "port"),
        actual("REGION", "event", "region"),
    ]

    assert match_paths(expected, returned, top_k=1)["rank_hits"] == 0
    assert match_paths(expected, returned, top_k=2)["rank_hits"] == 1


# ---------------------------------------------------------------------------
# 指标聚合
# ---------------------------------------------------------------------------


def test_metrics_aggregate_recall_and_precision_over_cases():
    """召回按标准路径汇总，精确率按返回路径汇总。"""
    exact_case = make_result(
        [path("REGION", "e", "r"), path("REGION", "e", "r2")],
        [actual("REGION", "e", "r"), actual("REGION", "e", "r2")],
    )
    noisy_case = make_result(
        [path("DIRECT", "e", "f")],
        [actual("DIRECT", "e", "f"), actual("SUPPLY", "s", "f")],
    )

    metrics = compute_metrics([exact_case, noisy_case])

    assert metrics["path_golden_total"] == 3
    assert metrics["path_recall"] == 1.0
    assert metrics["path_precision"] == 0.75
    assert metrics["path_rank_hit_rate"] == 1.0


def test_metrics_detect_missing_golden_path():
    """标准路径没被返回时召回率必须下降，指标不能恒为 100%。"""
    result = make_result(
        [path("REGION", "e", "r"), path("REGION", "e", "r2")],
        [actual("REGION", "e", "r")],
    )

    metrics = compute_metrics([result])

    assert metrics["path_recall"] == 0.5
    assert metrics["path_precision"] == 1.0


def test_security_cases_are_excluded_from_path_quality():
    """越权用例只考察拦截，不参与路径召回与精确。"""
    security_case = make_result(
        [path("REGION", "e", "r")],
        [],
        scenario="document_leak",
    )

    metrics = compute_metrics([security_case])

    assert metrics["path_golden_total"] == 0
    assert metrics["path_recall"] == 0.0


# ---------------------------------------------------------------------------
# 标准答案一致性
# ---------------------------------------------------------------------------


def test_cases_with_expected_paths_match_reference_implementation():
    """用例中的标准路径必须与参考实现算出的路径完全一致。"""
    for case in load_cases():
        expected_paths = case.get("expected_paths") or []
        if not expected_paths:
            continue

        gt = expected_paths_for_case(
            case["question"], case["role"], case.get("context")
        )
        reference = [
            {"rule": path["rule"], "node_ids": path["node_ids"]}
            for path in gt["paths"]
        ]

        assert expected_paths == reference, case["id"]


def test_every_case_with_path_requirement_has_standard_paths():
    """有路径期望的用例必须给出具体标准路径，不能只留条数。"""
    for case in load_cases():
        if case.get("expected_min_paths", 0) <= 0:
            continue
        assert case.get("expected_paths"), case["id"]
        assert len(case["expected_paths"]) == case["expected_min_paths"], case["id"]


def test_safety_and_held_out_cases_have_no_standard_paths():
    """越权与盲测用例不预置标准路径，避免用参考实现给自己出题。"""
    for case in load_cases():
        if case.get("scenario") in ("document_leak", "held_out"):
            assert not case.get("expected_paths"), case["id"]


def test_generated_standard_paths_are_marked_unreviewed():
    """草案必须标明来源，不能把自动生成的标准答案当成人工复核过的。"""
    marked = [
        case for case in load_cases() if case.get("expected_paths")
    ]
    assert marked
    for case in marked:
        assert case.get("expected_paths_source") == "generated", case["id"]
        assert case.get("expected_paths_reviewed") is False, case["id"]


def test_benchmark_endpoint_reports_path_quality():
    """评测接口必须输出路径召回、精确与排序指标，且不破坏原有指标。"""
    from app.api.routes.benchmark import run_benchmark

    metrics = run_benchmark()["metrics"]

    assert metrics["path_golden_total"] > 0
    assert 0.0 <= metrics["path_recall"] <= 1.0
    assert 0.0 <= metrics["path_precision"] <= 1.0
    assert 0.0 <= metrics["path_rank_hit_rate"] <= 1.0
    assert metrics["path_hit_rate"] == 1.0
    assert metrics["document_leak_count"] == 0
    assert metrics["edge_f1"] > 0
    assert metrics["path_provisional_case_total"] > 0


# ---------------------------------------------------------------------------
# 边级指标、排序质量与诊断
# ---------------------------------------------------------------------------


def test_edge_metrics_expose_broken_chain():
    """节点都在但中间关系断了时，节点召回满分、边召回下降。"""
    expected = [path("REGION", "A", "B", "C")]
    returned = [actual("REGION", "A", "B", "X", "C")]

    comparison = match_paths(expected, returned)

    assert comparison["golden_edge_total"] == 2
    assert comparison["matched_edge_total"] == 1
    assert comparison["edge_recall"] == 0.5
    assert comparison["edge_precision"] == pytest.approx(1 / 3)
    assert comparison["node_recall"] == 1.0
    assert comparison["golden_matched"] == 0


def test_mrr_uses_the_first_matching_position():
    """MRR 按每条标准路径第一次出现的名次计算。"""
    expected = [path("REGION", "A", "B"), path("REGION", "C", "D")]
    returned = [
        actual("PORT", "X", "Y"),
        actual("REGION", "A", "B"),
        actual("REGION", "C", "D"),
    ]

    comparison = match_paths(expected, returned)

    assert comparison["ranks"] == [2, 3]
    assert comparison["mrr"] == pytest.approx((1 / 2 + 1 / 3) / 2)


def test_diagnostics_list_missing_and_extra_paths():
    """报告需要能指出漏了哪条、多了哪条，而不只是一个比例。"""
    expected = [path("REGION", "A", "B"), path("REGION", "C", "D")]
    returned = [actual("REGION", "A", "B"), actual("PORT", "X", "Y")]

    comparison = match_paths(expected, returned)

    assert comparison["missing_golden"] == [
        {"rule": "REGION", "node_ids": ["C", "D"]}
    ]
    assert comparison["unmatched_actual"] == [
        {"rule": "PORT", "node_ids": ["X", "Y"]}
    ]


def test_wilson_interval_quantifies_small_sample_uncertainty():
    """19/19 不是稳定 100%，区间下界必须低于 1。"""
    interval = wilson_interval(19, 19)

    assert interval["high"] == 1.0
    assert interval["low"] < 1.0
    assert wilson_interval(0, 0) == {"low": 0.0, "high": 0.0}


def test_metrics_report_scenario_breakdown_and_review_scope():
    """场景分组和复核状态必须体现在汇总里。"""
    reviewed = make_result(
        [path("REGION", "A", "B")],
        [actual("REGION", "A", "B")],
    )
    reviewed["scenario"] = "single_event"
    reviewed["expected_paths_reviewed"] = True

    provisional = make_result(
        [path("DIRECT", "C", "D")],
        [actual("DIRECT", "C", "D")],
    )
    provisional["scenario"] = "event_pair"
    provisional["expected_paths_reviewed"] = False

    metrics = compute_metrics([reviewed, provisional])

    assert metrics["path_quality_scope"] == "mixed"
    assert metrics["path_reviewed_case_total"] == 1
    assert metrics["path_provisional_case_total"] == 1
    assert metrics["by_scenario"]["single_event"]["path_golden_total"] == 1
    assert metrics["by_scenario"]["event_pair"]["path_recall"] == 1.0
    assert metrics["edge_f1"] == 1.0
