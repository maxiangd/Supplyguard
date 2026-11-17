"""Benchmark 报告生成：Markdown + CSV 输出。"""

import json
import os
from datetime import datetime


def generate_markdown(results_path: str, output_path: str | None = None) -> str:
    """从 results.json 生成可读的 Markdown 报告。

    返回生成的 Markdown 内容。
    """
    with open(results_path, encoding="utf-8") as f:
        data = json.load(f)

    metrics = data.get("metrics", {})
    results = data.get("results", [])

    lines = [
        "# SupplyGuard Benchmark 报告",
        "",
        f"> 生成时间：{data.get('run_at', 'unknown')}",
        f"> 模式：{data.get('mode', 'unknown')}",
        f"> 用例数：{data.get('total_cases', 0)}",
        "> 越权口径：文档检索返回的每个块都必须包含提问角色的 allowed_roles；"
        "图谱对所有角色开放，不参与越权判定。",
        f"> 标准路径复核状态：{metrics.get('path_reviewed_case_total', 0)} 条已复核 / "
        f"{metrics.get('path_provisional_case_total', 0)} 条为自动生成草案"
        f"（质量口径：{metrics.get('path_quality_scope', 'provisional')}）。"
        "草案只能用于回归，不能当作正确性结论。",
        "",
        "---",
        "",
        "## 1. 汇总指标",
        "",
        "| 指标 | 值 | 说明 |",
        "|------|-----|------|",
        f"| 实体 Precision | {metrics.get('entity_precision', 0):.2%} | 正确命中的实体 / 返回的实体 |",
        f"| 实体 Recall | {metrics.get('entity_recall', 0):.2%} | 正确命中的实体 / 期望的实体 |",
        f"| 实体 F1 | {metrics.get('entity_f1', 0):.2%} | Precision 与 Recall 的调和平均 |",
        f"| 路径命中率 | {metrics.get('path_hit_rate', 0):.2%} | 实际路径数 ≥ 期望最少路径数的比例 |",
        f"| 路径召回率 | {metrics.get('path_recall', 0):.2%} | 标准路径被找到的比例（精确或包含） |",
        f"| 路径召回率 95% CI | {metrics.get('path_recall_ci', {}).get('low', 0):.2%} - {metrics.get('path_recall_ci', {}).get('high', 0):.2%} | 小样本下的区间估计，不是精确值 |",
        f"| 路径精确率 | {metrics.get('path_precision', 0):.2%} | 返回路径中属于标准答案的比例 |",
        f"| 路径精确命中率 | {metrics.get('path_exact_rate', 0):.2%} | 规则与节点序列完全不差的比例 |",
        f"| 节点 F1 | {metrics.get('node_f1', 0):.2%} | 节点集合层面的证据覆盖，不看顺序 |",
        f"| 边级 F1 | {metrics.get('edge_f1', 0):.2%} | 传导关系层面的覆盖，能发现节点都在但关系走错 |",
        f"| 路径 MRR | {metrics.get('path_mrr', 0):.3f} | 标准路径平均倒数名次，越接近 1 排得越靠前 |",
        f"| Recall@{metrics.get('path_rank_k', 6)}（按路径） | {metrics.get('path_rank_hit_rate', 0):.2%} | 标准路径进入提示词证据范围的比例 |",
        f"| Hit@{metrics.get('path_rank_k', 6)}（按用例） | {metrics.get('path_case_hit_rate', 0):.2%} | 每条用例前 {metrics.get('path_rank_k', 6)} 条里至少有一条标准路径的比例 |",
        f"| Hit@{metrics.get('path_rank_k', 6)} 95% CI | {metrics.get('path_hit_ci', {}).get('low', 0):.2%} - {metrics.get('path_hit_ci', {}).get('high', 0):.2%} | 小样本下的区间估计 |",
        f"| 规则 Precision | {metrics.get('rule_precision', 0):.2%} | 正确命中的传导规则 / 返回的规则 |",
        f"| 规则 Recall | {metrics.get('rule_recall', 0):.2%} | 正确命中的传导规则 / 期望的规则 |",
        f"| 多轮追问命中率 | {metrics.get('followup_success_rate', 0):.2%} | 继承上一轮实体且路径满足期望的比例 |",
        f"| 多轮用例通过 | {metrics.get('followup_case_passed', 0)} / {metrics.get('followup_case_total', 0)} | 通过 / 多轮用例总数 |",
        f"| **文档越权拦截率** | **{metrics.get('document_leak_block_rate', 0):.2%}** | 已检查用例中未返回角色不可见文档块的比例（模式：{metrics.get('document_leak_check_mode', 'dry_run')}） |",
        f"| 文档越权用例通过 | {metrics.get('document_leak_case_blocked', 0)} / {metrics.get('document_leak_case_checked', 0)} | 拦截成功 / 已检查用例数 |",
        f"| 文档越权用例总数 | {metrics.get('document_leak_case_total', 0)} | 含请求失败与 dry-run 用例 |",
        f"| 文档检索失败 | {metrics.get('document_leak_case_error', 0)} | 请求失败的用例，不计入拦截率分母 |",
        f"| 文档越权泄漏数 | {metrics.get('document_leak_count', 0)} | 返回了不可见文档块的用例条数 |",
        f"| 文档检索命中用例 | {metrics.get('document_leak_hit_cases', 0)} / {metrics.get('document_leak_case_total', 0)} | 至少返回一个块的用例数，0 命中说明拦截未真正被检验 |",
        "",
        "## 2. 按角色分组",
        "",
        "| 角色 | 用例数 | 文档泄漏数 | 路径命中率 | 路径召回 | 路径精确 |",
        "|------|--------|------------|-----------|----------|----------|",
    ]

    for role, d in sorted((metrics.get("by_role") or {}).items()):
        rate = f"{d['path_hit_rate']:.2%}" if d.get("path_total") else "—"
        recall = f"{d['path_recall']:.2%}" if d.get("path_golden_total") else "—"
        precision = f"{d['path_precision']:.2%}" if d.get("path_golden_total") else "—"
        lines.append(
            f"| {role} | {d['total']} | {d['leaks']} | {rate} | {recall} | {precision} |"
        )

    lines.extend([
        "",
        "## 3. 按场景分组",
        "",
        "| 场景 | 用例数 | 标准路径 | 路径召回 | 路径精确 | Recall@K | MRR | 边级 F1 |",
        "|------|--------|----------|----------|----------|----------|-----|---------|",
    ])

    for scenario, d in sorted((metrics.get("by_scenario") or {}).items()):
        if d.get("path_golden_total"):
            recall = f"{d['path_recall']:.2%}"
            precision = f"{d['path_precision']:.2%}"
            rank_hit = f"{d['path_rank_hit_rate']:.2%}"
            mrr = f"{d['path_mrr']:.3f}"
            edge_f1 = f"{d['edge_f1']:.2%}"
        else:
            recall = precision = rank_hit = edge_f1 = "—"
            mrr = "—"
        lines.append(
            f"| {scenario} | {d['total']} | {d['path_golden_total']} | "
            f"{recall} | {precision} | {rank_hit} | {mrr} | {edge_f1} |"
        )

    lines.extend([
        "",
        "## 4. 详细结果",
        "",
        "| ID | 场景 | 角色 | 问题 | 期望实体 | 实际实体 | 期望≥路径 | 实际路径 | 标准路径命中 | 缺口 | 多余 | 泄漏 |",
        "|----|------|------|------|----------|----------|-----------|----------|--------------|------|------|------|",
    ])

    for r in results:
        q = r.get("question", "")[:40]
        path_match = r.get("path_match") or {}
        if path_match.get("golden_total"):
            path_note = f"{path_match.get('golden_matched', 0)}/{path_match['golden_total']}"
        else:
            path_note = "—"
        missing = len(path_match.get("missing_golden") or [])
        unmatched = len(path_match.get("unmatched_actual") or [])
        lines.append(
            f"| {r.get('id', '?')} | {r.get('scenario', '?')} | {r.get('role', '?')} | {q} | "
            f"{len(r.get('expected_entities', []))} | {len(r.get('actual_entities', []))} | "
            f">{r.get('expected_min_paths', 0)} | {r.get('actual_paths', 0)} | "
            f"{path_note} | {missing} | {unmatched} | "
            f"{'⚠ YES' if r.get('document_role_leak') or r.get('leaked_document_ids') else 'OK'} |"
        )

    lines.extend([
        "",
        "## 5. 文档越权泄漏详情",
        "",
    ])

    leaks = [
        r
        for r in results
        if r.get("document_role_leak") or r.get("leaked_document_ids")
    ]
    if leaks:
        for r in leaks:
            documents = ", ".join(r.get("leaked_document_ids", [])) or "未知"
            lines.append(
                f"- **{r['id']}** ({r['role']}): {r['question']} —— 泄漏文档：{documents}"
            )
    else:
        lines.append("✅ 未发现文档越权泄漏")
        lines.append("")

    lines.extend([
        "",
        "---",
        "",
        f"*报告由 tests/benchmark/report.py 自动生成于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*",
    ])

    content = "\n".join(lines)

    if output_path:
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"Markdown 报告已保存到 {output_path}")

    return content


if __name__ == "__main__":
    import sys
    input_path = sys.argv[1] if len(sys.argv) > 1 else "tests/benchmark/results.json"
    output_path = sys.argv[2] if len(sys.argv) > 2 else "tests/benchmark/report.md"
    report = generate_markdown(input_path, output_path)
    print(report)