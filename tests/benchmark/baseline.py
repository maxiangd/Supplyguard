"""单 Agent 基线：每个用例只调用一次模型，不做检索、图谱与权限过滤。

用途是与固定工作流对比：基线只拿到问题、角色和实体清单，
让它直接猜受影响实体；路径类指标天然为 0，说明「多跳传导」不是
模型单独能稳定给出的能力。文档越权用例不调用模型，标记为未检查。
"""

import argparse
import json
import os

from app.core.configuration import settings
from app.services.llm import get_chat_client
from tests.benchmark.metrics import compute_metrics
from tests.benchmark.path_match import match_paths

_GRAPH_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mini_graph.json")

BASELINE_PROMPT = """你是供应链风险分析的单个 Agent。下面是可以引用的实体清单（ID：名称）。

{entities}

问题：{question}
提问角色：{role}

只输出 JSON，不要解释：{{"entity_ids": ["受影响实体的 ID"]}}
只允许使用清单里出现的 ID；不确定时返回空数组。"""


def _load_catalog() -> tuple[str, set[str]]:
    """读取实体清单文本与合法 ID 集合。"""
    with open(_GRAPH_PATH, encoding="utf-8") as f:
        graph = json.load(f)
    nodes = graph.get("nodes", [])
    lines = [f"- {node['id']}：{node['name']}（{node.get('type', '')}）" for node in nodes]
    return "\n".join(lines), {node["id"] for node in nodes}


def _parse_entity_ids(text: str | None, known_ids: set[str]) -> list[str]:
    """从模型输出里提取合法实体 ID，忽略未知 ID 与格式噪声。"""
    if not text:
        return []
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    raw = data.get("entity_ids") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if str(item) in known_ids]


def _baseline_case(
    case: dict,
    catalog: str,
    known_ids: set[str],
    model: str,
    client_factory,
) -> dict:
    """跑一条用例并组装成与 runner 相同的结果结构。"""
    actual_entities: list[str] = []
    error = None
    if case.get("scenario") != "document_leak":
        try:
            response = client_factory(model).chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": BASELINE_PROMPT.format(
                            entities=catalog,
                            question=case["question"],
                            role=case["role"],
                        ),
                    }
                ],
                temperature=0,
                response_format={"type": "json_object"},
            )
            actual_entities = _parse_entity_ids(
                response.choices[0].message.content, known_ids
            )
        except Exception as exc:  # 基线失败不阻塞整个评测
            error = str(exc)

    return {
        "id": case["id"],
        "scenario": case.get("scenario", ""),
        "question": case["question"],
        "role": case["role"],
        "context": case.get("context", []),
        "expected_entities": case.get("expected_entities", []),
        "actual_entities": actual_entities,
        "expected_min_paths": 0,
        "actual_paths": 0,
        "expected_path_rules": case.get("expected_path_rules", []),
        "actual_path_rules": [],
        "expected_paths": case.get("expected_paths", []),
        "expected_paths_reviewed": case.get("expected_paths_reviewed", False),
        "actual_path_details": [],
        "path_match": match_paths(case.get("expected_paths", []), []),
        "inherited_entity_ids": [],
        "document_hits": 0,
        "document_role_leak": False,
        "leaked_document_ids": [],
        "document_check_mode": "baseline",
        "baseline_error": error,
    }


def run_baseline(
    cases: list[dict],
    model: str | None = None,
    limit: int = 10,
    role: str | None = None,
    client_factory=get_chat_client,
) -> dict:
    """跑单 Agent 基线，返回与正式评测同口径的指标。"""
    model = model or settings.llm_model
    selected = [case for case in cases if not role or case["role"] == role][:limit]
    catalog, known_ids = _load_catalog()

    results = [
        _baseline_case(case, catalog, known_ids, model, client_factory)
        for case in selected
    ]
    return {
        "status": "measured",
        "model": model,
        "total_cases": len(results),
        "metrics": compute_metrics(results),
        "results": results,
    }


def main() -> None:
    """命令行入口：python -m tests.benchmark.baseline --limit 10。"""
    parser = argparse.ArgumentParser(description="单 Agent 基线评测")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--role", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--output", default="tests/benchmark/baseline_results.json")
    args = parser.parse_args()

    cases_path = os.path.join(os.path.dirname(_GRAPH_PATH), "cases.json")
    with open(cases_path, encoding="utf-8") as f:
        cases = json.load(f)

    payload = run_baseline(cases, model=args.model, limit=args.limit, role=args.role)
    metrics = payload["metrics"]
    print(
        f"基线完成：{payload['total_cases']} 条，"
        f"实体 F1 {metrics.get('entity_f1', 0):.2%}，"
        f"路径命中率 {metrics.get('path_hit_rate', 0):.2%}"
    )
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"已写入 {args.output}")


if __name__ == "__main__":
    main()
