"""越权防护回归测试。

防越权率的判据：任何角色、任何问题，返回结果里都不能出现该角色不可见的节点。
这里用 benchmark 的 ground truth 做全组合探测，并用构造的泄漏样本确认指标
不是恒等于 100%（防止指标本身失效后测试仍然全绿）。
"""

import json
from pathlib import Path

import pytest

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


def test_benchmark_endpoint_reports_full_block_rate():
    """评测接口必须报告 100% 越权拦截率且泄漏数为 0。"""
    from app.api.routes.benchmark import run_benchmark

    data = run_benchmark()
    metrics = data["metrics"]

    assert metrics["leak_count"] == 0
    assert metrics["leak_case_total"] > 0
    assert metrics["leak_case_blocked"] == metrics["leak_case_total"]
    assert metrics["leak_block_rate"] == 1.0


def test_metric_detects_real_leak():
    """指标必须能识别真实泄漏，而不是恒为 100%。"""
    leaked = {
        "scenario": "cross_role_leak",
        "role": "finance",
        "expected_entities": [],
        "actual_entities": [],
        "expected_min_paths": 0,
        "actual_paths": 1,
        "role_leak": True,
        "leaked_node_ids": ["supplier-enjie"],
    }
    blocked = {
        "scenario": "cross_role_leak",
        "role": "finance",
        "expected_entities": [],
        "actual_entities": [],
        "expected_min_paths": 0,
        "actual_paths": 0,
        "role_leak": False,
        "leaked_node_ids": [],
    }

    metrics = compute_metrics([leaked, blocked])

    assert metrics["leak_count"] == 1
    assert metrics["leak_block_rate"] == 0.5


def test_leaked_node_ids_detects_violation():
    """路径中混入不可见节点时必须被检出。"""
    paths = [{"node_ids": ["supplier-enjie"]}]

    assert leaked_node_ids(paths, "logistics") == ["supplier-enjie"]
    assert leaked_node_ids(paths, "procurement") == []
