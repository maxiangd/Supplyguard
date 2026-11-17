"""越权防护回归测试。

防越权率的判据：任何角色、任何问题，返回结果里都不能出现该角色不可见的节点。
这里用 benchmark 的 ground truth 做全组合探测，并用构造的泄漏样本确认指标
不是恒等于 100%（防止指标本身失效后测试仍然全绿）。
"""

import json
from pathlib import Path

import pytest

from tests.benchmark import ground_truth
from tests.benchmark.ground_truth import expected_paths_for_case, leaked_node_ids
from tests.benchmark.metrics import compute_metrics
from tests.benchmark.seed_data import NODES

CASES_PATH = Path(__file__).parent / "benchmark" / "cases.json"
ROLES = ["admin", "procurement", "logistics", "quality", "finance"]


def load_cases() -> list[dict]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", load_cases(), ids=lambda case: case["id"])
def test_case_role_never_leaks(case):
    """每条用例用它的原始角色提问，都不能返回不可见节点。"""
    gt = expected_paths_for_case(case["question"], case["role"])
    assert gt["leaked_node_ids"] == [], f"{case['id']} 泄漏：{gt['leaked_node_ids']}"


@pytest.mark.parametrize("role", ROLES)
def test_every_question_is_safe_for_every_role(role):
    """跨角色探测：用每个角色去问全部问题，模拟越权尝试。"""
    for case in load_cases():
        gt = expected_paths_for_case(case["question"], role)
        assert gt["leaked_node_ids"] == [], (
            f"{case['id']} 用角色 {role} 提问时泄漏 {gt['leaked_node_ids']}"
        )


def test_app_visibility_matches_seed_data():
    """真实链路的 _visible 判定必须与图谱 seed 数据一致。"""
    from app.services.graph import _visible

    for node in NODES:
        for role in ROLES:
            expected = role == "admin" or role in (node.get("allowed_roles") or [])
            assert _visible(node, role) is expected, f"{node['id']} / {role}"


def test_benchmark_endpoint_reports_document_block_rate():
    """评测接口必须报告文档越权用例，并在 dry-run 下标明未实际检索。"""
    from app.api.routes.benchmark import run_benchmark

    data = run_benchmark()
    metrics = data["metrics"]

    assert metrics["document_leak_count"] == 0
    assert metrics["document_leak_case_total"] > 0
    assert (
        metrics["document_leak_case_blocked"]
        == metrics["document_leak_case_total"]
    )
    assert metrics["document_leak_block_rate"] == 1.0
    assert metrics["document_leak_check_mode"] == "dry_run"
    assert metrics["document_leak_hit_cases"] == 0


def test_metric_detects_real_document_leak():
    """指标必须能识别真实文档泄漏，而不是恒为 100%。"""
    leaked = {
        "scenario": "document_leak",
        "role": "finance",
        "expected_entities": [],
        "actual_entities": [],
        "expected_min_paths": 0,
        "actual_paths": 0,
        "document_hits": 3,
        "document_role_leak": True,
        "leaked_document_ids": ["doc-quality-report"],
        "document_check_mode": "api",
    }
    blocked = {
        "scenario": "document_leak",
        "role": "finance",
        "expected_entities": [],
        "actual_entities": [],
        "expected_min_paths": 0,
        "actual_paths": 0,
        "document_hits": 2,
        "document_role_leak": False,
        "leaked_document_ids": [],
        "document_check_mode": "api",
    }

    metrics = compute_metrics([leaked, blocked])

    assert metrics["document_leak_count"] == 1
    assert metrics["document_leak_block_rate"] == 0.5
    assert metrics["document_leak_hit_cases"] == 2
    assert metrics["document_leak_check_mode"] == "api"


def test_zero_hit_cases_do_not_count_as_meaningful_probe():
    """返回 0 个文档块算拦截成功，但不计入“真正检验过”的用例数。"""
    empty = {
        "scenario": "document_leak",
        "role": "logistics",
        "expected_entities": [],
        "actual_entities": [],
        "expected_min_paths": 0,
        "actual_paths": 0,
        "document_hits": 0,
        "document_role_leak": False,
        "leaked_document_ids": [],
        "document_check_mode": "api",
    }

    metrics = compute_metrics([empty])

    assert metrics["document_leak_block_rate"] == 1.0
    assert metrics["document_leak_hit_cases"] == 0


def test_failed_requests_do_not_inflate_block_rate():
    """请求失败的用例不能当成拦截成功，也不进分母。"""
    failed = {
        "scenario": "document_leak",
        "role": "finance",
        "expected_entities": [],
        "actual_entities": [],
        "expected_min_paths": 0,
        "actual_paths": 0,
        "document_hits": 0,
        "document_role_leak": False,
        "leaked_document_ids": [],
        "document_check_mode": "error",
    }

    metrics = compute_metrics([failed])

    assert metrics["document_leak_check_mode"] == "error"
    assert metrics["document_leak_case_checked"] == 0
    assert metrics["document_leak_case_error"] == 1
    assert metrics["document_leak_block_rate"] == 0.0


def test_leaked_node_ids_detects_violation(monkeypatch):
    """路径中混入不可见节点时必须被检出。

    图谱当前对所有角色开放，这里用临时构造的受限节点验证检测逻辑本身没坏，
    将来若重新收紧图谱权限仍然有效。
    """
    monkeypatch.setattr(
        ground_truth,
        "NODES",
        [{"id": "supplier-enjie", "allowed_roles": ["procurement"]}],
    )
    paths = [{"node_ids": ["supplier-enjie"]}]

    assert leaked_node_ids(paths, "logistics") == ["supplier-enjie"]
    assert leaked_node_ids(paths, "procurement") == []
