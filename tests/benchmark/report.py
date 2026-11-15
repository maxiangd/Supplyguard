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
        "> 越权口径：返回结果不含提问角色不可见的节点即视为拦截成功；"
        "问题命中可见实体时返回路径属于正常召回。",
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
        f"| 规则 Precision | {metrics.get('rule_precision', 0):.2%} | 正确命中的传导规则 / 返回的规则 |",
        f"| 规则 Recall | {metrics.get('rule_recall', 0):.2%} | 正确命中的传导规则 / 期望的规则 |",
        f"| 多轮追问命中率 | {metrics.get('followup_success_rate', 0):.2%} | 继承上一轮实体且路径满足期望的比例 |",
        f"| 多轮用例通过 | {metrics.get('followup_case_passed', 0)} / {metrics.get('followup_case_total', 0)} | 通过 / 多轮用例总数 |",
        f"| **越权拦截率** | **{metrics.get('leak_block_rate', 0):.2%}** | 未返回角色不可见节点的越权用例占比 |",
        f"| 越权用例通过 | {metrics.get('leak_case_blocked', 0)} / {metrics.get('leak_case_total', 0)} | 拦截成功 / 越权用例总数 |",
        f"| 越权泄漏数 | {metrics.get('leak_count', 0)} | 所有用例中返回了不可见节点的条数 |",
        "",
        "## 2. 按角色分组",
        "",
        "| 角色 | 用例数 | 泄漏数 | 路径命中率 |",
        "|------|--------|--------|-----------|",
    ]

    for role, d in sorted((metrics.get("by_role") or {}).items()):
        rate = f"{d['path_hit_rate']:.2%}" if d.get("path_total") else "—"
        lines.append(
            f"| {role} | {d['total']} | {d['leaks']} | {rate} |"
        )

    lines.extend([
        "",
        "## 3. 详细结果",
        "",
        "| ID | 场景 | 角色 | 问题 | 期望实体 | 实际实体 | 期望≥路径 | 实际路径 | 泄漏 |",
        "|----|------|------|------|----------|----------|-----------|----------|------|",
    ])

    for r in results:
        q = r.get("question", "")[:40]
        lines.append(
            f"| {r.get('id', '?')} | {r.get('scenario', '?')} | {r.get('role', '?')} | {q} | "
            f"{len(r.get('expected_entities', []))} | {len(r.get('actual_entities', []))} | "
            f">{r.get('expected_min_paths', 0)} | {r.get('actual_paths', 0)} | "
            f"{'⚠ YES' if r.get('role_leak') else 'OK'} |"
        )

    lines.extend([
        "",
        "## 4. 越权泄漏详情",
        "",
    ])

    leaks = [r for r in results if r.get("role_leak") or r.get("leaked_node_ids")]
    if leaks:
        for r in leaks:
            nodes = ", ".join(r.get("leaked_node_ids", [])) or "未知"
            lines.append(f"- **{r['id']}** ({r['role']}): {r['question']} —— 泄漏节点：{nodes}")
    else:
        lines.append("✅ 未发现越权泄漏")
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