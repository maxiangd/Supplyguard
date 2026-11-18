"""Benchmark API：用例列表 + 运行评测（dry-run 模式，不调 LLM）。"""

import json
import os
import sys
import time

from fastapi import APIRouter

# 确保项目根目录在 sys.path 中
# __file__ = app/api/routes/benchmark.py → 4 级 dirname 回到项目根
_BENCHMARK_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, _BENCHMARK_DIR)

from tests.benchmark.baseline import run_baseline
from tests.benchmark.ground_truth import expected_paths_for_case
from tests.benchmark.metrics import compute_metrics
from tests.benchmark.path_match import match_paths

router = APIRouter(prefix="/benchmark", tags=["benchmark"])

_CASES_PATH = os.path.join(_BENCHMARK_DIR, "tests", "benchmark", "cases.json")


@router.get("/cases")
def list_cases() -> dict:
    """返回全部 benchmark 用例。"""
    if not os.path.exists(_CASES_PATH):
        return {"cases": [], "total": 0}

    with open(_CASES_PATH, encoding="utf-8") as f:
        cases = json.load(f)

    return {
        "cases": [
            {
                "id": c["id"],
                "scenario": c.get("scenario", ""),
                "question": c["question"],
                "role": c["role"],
                "expected_entities": c.get("expected_entities", []),
                "expected_path_rules": c.get("expected_path_rules", []),
                "expected_min_paths": c.get("expected_min_paths", 0),
                "expected_paths": c.get("expected_paths", []),
                "expected_paths_reviewed": c.get("expected_paths_reviewed", False),
                "notes": c.get("notes", ""),
            }
            for c in cases
        ],
        "total": len(cases),
    }


@router.post("/run")
def run_benchmark(include_baseline: bool = False, baseline_limit: int = 10) -> dict:
    """运行 ground truth benchmark（不调 LLM/Qdrant/Neo4j）。

    用纯 Python 传导规则 + 实体匹配作为 ground truth，
    与用例中的期望值对比，返回完整指标与详细结果。
    """
    started_at = time.strftime("%Y-%m-%d %H:%M:%S")

    if not os.path.exists(_CASES_PATH):
        return {"error": "cases.json not found", "ran_at": started_at}

    with open(_CASES_PATH, encoding="utf-8") as f:
        cases = json.load(f)

    results = []
    for case in cases:
        question = case["question"]
        role = case["role"]
        gt = expected_paths_for_case(question, role, case.get("context"))

        actual_rules = list(set(p["rule"] for p in gt["paths"]))
        actual_entities = gt["entity_ids"]
        actual_path_details = [
            {"rule": path["rule"], "node_ids": path["node_ids"]}
            for path in gt["paths"]
        ]
        is_document_case = case.get("scenario") == "document_leak"

        results.append({
            "id": case["id"],
            "scenario": case.get("scenario", ""),
            "question": question,
            "role": role,
            "context": case.get("context", []),
            "expected_entities": case.get("expected_entities", []),
            "actual_entities": [] if is_document_case else actual_entities,
            "expected_min_paths": case.get("expected_min_paths", 0),
            "actual_paths": 0 if is_document_case else gt["path_count"],
            "expected_path_rules": case.get("expected_path_rules", []),
            "actual_path_rules": [] if is_document_case else actual_rules,
            "expected_paths": case.get("expected_paths", []),
            "expected_paths_reviewed": case.get("expected_paths_reviewed", False),
            # 这里用参考实现算出的路径作为“实际返回”，用于校验标准答案与规则实现是否一致
            "actual_path_details": [] if is_document_case else actual_path_details,
            "path_match": match_paths(
                case.get("expected_paths", []),
                [] if is_document_case else actual_path_details,
            ),
            "inherited_entity_ids": gt["inherited_entity_ids"],
            # 文档拦截需要真实检索接口，这里只标记 dry-run 模式
            "document_hits": 0,
            "document_role_leak": False,
            "leaked_document_ids": [],
            "document_check_mode": "dry_run",
        })

    metrics = compute_metrics(results)

    baseline = (
        run_baseline(cases, limit=baseline_limit)
        if include_baseline
        else {"status": "not_run"}
    )

    return {
        "ran_at": started_at,
        "total_cases": len(cases),
        "metrics": metrics,
        "baseline": baseline,
        "results": [
            _case_result(r) for r in results
        ],
    }


def _case_result(result: dict) -> dict:
    """单条用例的通过判定。

    文档越权用例只看检索结果里有没有角色不可见的文档块；
    其余用例看路径数与实体是否命中。
    """
    if result["scenario"] == "document_leak":
        leaked = result.get("leaked_document_ids", [])
        mode = result.get("document_check_mode")
        if mode == "api":
            note = (
                "已拦截：未返回角色不可见文档"
                if not leaked
                else f"泄漏不可见文档：{', '.join(leaked)}"
            )
            return {"id": result["id"], "passed": not leaked, "note": note}
        if mode == "error":
            return {
                "id": result["id"],
                "passed": False,
                "note": "检索请求失败，未计入拦截率",
            }
        return {
            "id": result["id"],
            "passed": True,
            "note": "dry-run 未实际检索文档，需调真实接口验证",
        }

    if result["scenario"] == "multi_turn":
        inherited = result.get("inherited_entity_ids", [])
        passed = (
            bool(inherited)
            and result["actual_paths"] >= result["expected_min_paths"]
            and set(result["expected_entities"]) <= set(result["actual_entities"])
        )
        return {
            "id": result["id"],
            "passed": passed,
            "note": (
                f"继承实体 {', '.join(inherited)}，{result['actual_paths']} 条路径"
                if inherited
                else "未继承上一轮实体"
            ),
        }

    passed = (
        result["actual_paths"] >= result["expected_min_paths"]
        and set(result["expected_entities"]) <= set(result["actual_entities"])
    )
    return {
        "id": result["id"],
        "passed": passed,
        "note": (
            f"期望≥{result['expected_min_paths']}条路径，实际{result['actual_paths']}条"
            if result["expected_min_paths"] > 0
            else "未命中图谱实体"
        ),
    }
