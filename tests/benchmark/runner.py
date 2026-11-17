"""Benchmark 运行器：用 ground truth 对比真实链路输出。

用法：
  python -m tests.benchmark.runner          # 运行全部用例
  python -m tests.benchmark.runner --role procurement  # 只跑指定角色
  python -m tests.benchmark.runner --scenario document_leak  # 只跑文档越权用例
  python -m tests.benchmark.runner --dry-run  # 只跑 ground truth，不调真实链路
"""

import argparse
import json
import os
import sys
import time
from typing import Any

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.benchmark.ground_truth import expected_paths_for_case
from tests.benchmark.metrics import compute_metrics
from tests.benchmark.path_match import match_paths


def run_ground_truth_only(cases: list[dict]) -> list[dict]:
    """只跑 ground truth（不调真实链路），验证用例与预期一致性。"""
    results = []
    for case in cases:
        gt = expected_paths_for_case(
            case["question"], case["role"], case.get("context")
        )
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
            "question": case["question"],
            "role": case["role"],
            "context": case.get("context", []),
            "expected_entities": case.get("expected_entities", []),
            "actual_entities": [] if is_document_case else actual_entities,
            "expected_min_paths": case.get("expected_min_paths", 0),
            "actual_paths": 0 if is_document_case else len(gt["paths"]),
            "expected_path_rules": case.get("expected_path_rules", []),
            "actual_path_rules": [] if is_document_case else actual_rules,
            "expected_paths": case.get("expected_paths", []),
            "expected_paths_reviewed": case.get("expected_paths_reviewed", False),
            # 参考实现算出的路径就是这一模式下的“真实返回”，用于校验标准答案本身
            "actual_path_details": [] if is_document_case else actual_path_details,
            "path_match": match_paths(
                case.get("expected_paths", []),
                [] if is_document_case else actual_path_details,
            ),
            "inherited_entity_ids": gt["inherited_entity_ids"],
            # dry-run 不连接 Qdrant，文档拦截在真实接口模式下才有区分度
            "document_hits": 0,
            "document_role_leak": False,
            "leaked_document_ids": [],
            "document_check_mode": "dry_run",
            "gt_paths": gt["paths"],
        })
    return results


def run_against_real_api(cases: list[dict], base_url: str = "http://127.0.0.1:8000") -> list[dict]:
    """调真实后端 API 运行 Benchmark。

    对每个用例：
    1. 登录获取 token
    2. 调 POST /api/graph/impact 获取图谱结果
    3. 对比 ground truth
    """
    import urllib.request
    import urllib.error

    # 本地评测请求绕过系统代理，避免 clash 之类把 127.0.0.1 也转发出去
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # 缓存各角色的 token
    tokens: dict[str, str] = {}

    def get_token(role: str) -> str:
        if role not in tokens:
            username = f"catl_{role}"
            body = json.dumps({"username": username, "password": "catl123"}).encode()
            req = urllib.request.Request(
                f"{base_url}/api/auth/login",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            try:
                with opener.open(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode())
                    tokens[role] = data["access_token"]
            except Exception as e:
                print(f"  [WARN] 登录失败 ({role}): {e}")
                tokens[role] = ""
        return tokens[role]

    results = []

    def call_impact(token: str, payload: dict) -> dict:
        """POST /api/graph/impact，把请求细节收在一处。"""
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{base_url}/api/graph/impact",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )
        with opener.open(req, timeout=30) as resp:
            return json.loads(resp.read().decode())

    def call_retrieval(token: str, payload: dict) -> dict:
        """POST /api/retrieval/search，文档越权用例走这里。"""
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{base_url}/api/retrieval/search",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )
        with opener.open(req, timeout=30) as resp:
            return json.loads(resp.read().decode())

    for i, case in enumerate(cases):
        print(f"\n[{i + 1}/{len(cases)}] {case['id']}: {case['question'][:50]}... (role={case['role']})")
        role = case["role"]
        token = get_token(role)
        if not token:
            print("  跳过（无 token）")
            continue

        inherited_entity_ids: list[str] = []
        try:
            if case.get("scenario") == "document_leak":
                data = call_retrieval(
                    token, {"query": case["question"], "top_k": 5}
                )
            elif case.get("context"):
                # 多轮追问：先用上一轮问题拿到实体，再用实体 ID 模拟继承
                previous = call_impact(
                    token, {"question": case["context"][-1]["question"]}
                )
                inherited_entity_ids = [
                    entity["id"] for entity in previous.get("entities", [])
                ][:3]
                if inherited_entity_ids:
                    data = call_impact(
                        token, {"entity_id": inherited_entity_ids[0], "max_hops": 3}
                    )
                else:
                    data = {"entities": [], "paths": []}
            else:
                data = call_impact(token, {"question": case["question"]})
        except urllib.error.HTTPError as e:
            print(f"  HTTP {e.code}: {e.reason}")
            results.append({
                "id": case["id"],
                "scenario": case.get("scenario", ""),
                "question": case["question"],
                "role": role,
                "context": case.get("context", []),
                "expected_entities": case.get("expected_entities", []),
                "actual_entities": [],
                "expected_min_paths": case.get("expected_min_paths", 0),
                "actual_paths": 0,
                "expected_path_rules": case.get("expected_path_rules", []),
                "actual_path_rules": [],
                "expected_paths": case.get("expected_paths", []),
                "expected_paths_reviewed": case.get("expected_paths_reviewed", False),
                "actual_path_details": [],
                "path_match": match_paths(case.get("expected_paths", []), []),
                "inherited_entity_ids": inherited_entity_ids,
                "document_hits": 0,
                "document_role_leak": False,
                "leaked_document_ids": [],
                "document_check_mode": (
                    "error" if case.get("scenario") == "document_leak" else ""
                ),
                "api_error": f"HTTP {e.code}",
            })
            continue
        except Exception as e:
            print(f"  请求失败: {e}")
            continue

        # 文档越权用例：核对返回块的 allowed_roles，不进入图谱路径统计
        if case.get("scenario") == "document_leak":
            hits = data.get("hits") or []
            leaked_document_ids = [
                hit.get("document_id") or hit.get("filename") or "?"
                for hit in hits
                if role != "admin"
                and role not in (hit.get("allowed_roles") or [])
            ]
            results.append({
                "id": case["id"],
                "scenario": case.get("scenario", ""),
                "question": case["question"],
                "role": role,
                "context": case.get("context", []),
                "expected_entities": [],
                "actual_entities": [],
                "expected_min_paths": 0,
                "actual_paths": 0,
                "expected_path_rules": [],
                "actual_path_rules": [],
                "expected_paths": [],
                "expected_paths_reviewed": False,
                "actual_path_details": [],
                "path_match": match_paths([], []),
                "inherited_entity_ids": [],
                "document_hits": len(hits),
                "document_role_leak": bool(leaked_document_ids),
                "leaked_document_ids": leaked_document_ids,
                "document_check_mode": "api",
            })
            print(
                f"  文档命中: {len(hits)} | 泄漏: {'YES' if leaked_document_ids else 'NO'}"
            )
            time.sleep(0.3)
            continue

        actual_entities = [e["id"] for e in data.get("entities", [])]
        actual_paths_list = data.get("paths", [])
        actual_rules = list(set(p["rule"] for p in actual_paths_list))

        # 路径质量比对需要节点序列，不能只保留条数
        actual_path_details = [
            {
                "rule": path.get("rule", ""),
                "node_ids": [node.get("id", "") for node in path.get("nodes", [])],
            }
            for path in actual_paths_list
        ]

        result = {
            "id": case["id"],
            "scenario": case.get("scenario", ""),
            "question": case["question"],
            "role": role,
            "context": case.get("context", []),
            "expected_entities": case.get("expected_entities", []),
            "actual_entities": actual_entities,
            "expected_min_paths": case.get("expected_min_paths", 0),
            "actual_paths": len(actual_paths_list),
            "expected_path_rules": case.get("expected_path_rules", []),
            "actual_path_rules": actual_rules,
            "expected_paths": case.get("expected_paths", []),
            "expected_paths_reviewed": case.get("expected_paths_reviewed", False),
            "actual_path_details": actual_path_details,
            "path_match": match_paths(case.get("expected_paths", []), actual_path_details),
            "inherited_entity_ids": inherited_entity_ids,
        }
        results.append(result)

        # 简要输出
        entity_ok = set(case.get("expected_entities", [])) <= set(actual_entities)
        path_ok = len(actual_paths_list) >= case.get("expected_min_paths", 0)
        print(f"  实体: {'OK' if entity_ok else 'MISS'} | 路径: {len(actual_paths_list)} (>={case.get('expected_min_paths', 0)} {'OK' if path_ok else 'FAIL'})")

        # 避免请求太快
        time.sleep(0.3)

    return results


def main():
    parser = argparse.ArgumentParser(description="SupplyGuard Benchmark Runner")
    parser.add_argument("--role", help="只跑指定角色")
    parser.add_argument("--scenario", help="只跑某类场景")
    parser.add_argument("--dry-run", action="store_true", help="只跑 ground truth，不调 API")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="API 地址")
    parser.add_argument("--output", default="tests/benchmark/results.json", help="结果输出路径")
    parser.add_argument("--min-path-recall", type=float, help="路径召回率低于该值时以非零码退出")
    parser.add_argument("--min-edge-f1", type=float, help="边级 F1 低于该值时以非零码退出")
    parser.add_argument("--min-hit-rate", type=float, help="Hit@K 低于该值时以非零码退出")
    args = parser.parse_args()

    # 加载用例
    cases_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "cases.json"
    )
    with open(cases_path, encoding="utf-8") as f:
        cases = json.load(f)

    # 过滤
    if args.role:
        cases = [c for c in cases if c["role"] == args.role]
    if args.scenario:
        cases = [c for c in cases if c.get("scenario") == args.scenario]

    print(f"加载 {len(cases)} 条用例")

    if args.dry_run:
        print("模式: ground truth only（不调 API）")
        results = run_ground_truth_only(cases)
    else:
        print(f"模式: 调真实 API ({args.base_url})")
        results = run_against_real_api(cases, args.base_url)

    # 计算指标
    metrics = compute_metrics(results)

    # 输出
    report = {
        "run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": "dry_run" if args.dry_run else "api",
        "total_cases": len(cases),
        "total_results": len(results),
        "metrics": metrics,
        "results": results,
    }

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"\n{'=' * 60}")
    print(f"Benchmark 完成：{len(results)} 条结果")
    print(f"实体 Precision: {metrics['entity_precision']:.2%}")
    print(f"实体 Recall:    {metrics['entity_recall']:.2%}")
    print(f"实体 F1:        {metrics['entity_f1']:.2%}")
    print(f"路径命中率:     {metrics['path_hit_rate']:.2%}")
    print(f"路径召回率:     {metrics['path_recall']:.2%}  (95% CI {metrics['path_recall_ci']['low']:.2%} - {metrics['path_recall_ci']['high']:.2%})")
    print(f"路径精确率:     {metrics['path_precision']:.2%}")
    print(f"边级 F1:        {metrics['edge_f1']:.2%}")
    print(f"路径 MRR:       {metrics['path_mrr']:.4f}")
    print(f"前 {metrics['path_rank_k']} 条命中: {metrics['path_rank_hit_rate']:.2%}")
    print(f"Hit@{metrics['path_rank_k']}:         {metrics['path_case_hit_rate']:.2%}  (95% CI {metrics['path_hit_ci']['low']:.2%} - {metrics['path_hit_ci']['high']:.2%})")
    print(f"标准路径复核:   {metrics['path_reviewed_case_total']} 已复核 / {metrics['path_provisional_case_total']} 草案")
    print(f"规则 Precision: {metrics['rule_precision']:.2%}")
    print(f"文档越权拦截率: {metrics['document_leak_block_rate']:.2%}  (模式 {metrics['document_leak_check_mode']})")
    print(f"文档拦截已检查: {metrics['document_leak_case_checked']} 条 | 请求失败 {metrics['document_leak_case_error']} 条")
    print(f"文档泄漏:       {metrics['document_leak_count']} 条")
    print(f"文档命中用例:   {metrics['document_leak_hit_cases']} / {metrics['document_leak_case_total']}")
    print(f"\n结果已保存到 {args.output}")

    gates = [
        ("路径召回率", args.min_path_recall, metrics["path_recall"]),
        ("边级 F1", args.min_edge_f1, metrics["edge_f1"]),
        ("Hit@K", args.min_hit_rate, metrics["path_case_hit_rate"]),
    ]
    failed = [
        (name, threshold, actual)
        for name, threshold, actual in gates
        if threshold is not None and actual < threshold
    ]
    if failed:
        print("\n评测未达门槛：")
        for name, threshold, actual in failed:
            print(f"  {name}: {actual:.2%} < {threshold:.2%}")
        sys.exit(1)


if __name__ == "__main__":
    main()