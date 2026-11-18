"""单 Agent 基线测试：解析合法 ID、不调用模型处理越权用例。"""

from types import SimpleNamespace

from tests.benchmark import baseline


def _fake_factory(payload: str):
    """构造返回固定内容的假模型客户端。"""

    def factory(_model: str):
        def create(**_kwargs):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(message=SimpleNamespace(content=payload))
                ]
            )

        return SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )

    return factory


def test_baseline_keeps_only_known_entity_ids():
    case = {
        "id": "case_x",
        "scenario": "single_event",
        "question": "宜春停产影响什么？",
        "role": "procurement",
        "expected_entities": ["event-yichun-suspend"],
        "expected_paths": [],
    }
    payload = '{"entity_ids": ["event-yichun-suspend", "made-up-id"]}'
    result = baseline.run_baseline(
        [case], model="fake", limit=1, client_factory=_fake_factory(payload)
    )
    assert result["status"] == "measured"
    assert result["results"][0]["actual_entities"] == ["event-yichun-suspend"]
    assert result["metrics"]["entity_f1"] == 1.0
    assert result["metrics"]["path_hit_rate"] == 0.0


def test_baseline_skips_llm_for_document_cases():
    case = {
        "id": "case_doc",
        "scenario": "document_leak",
        "question": "财务合同条款？",
        "role": "procurement",
        "expected_entities": [],
        "expected_paths": [],
    }

    def exploding_factory(_model):
        raise AssertionError("越权用例不应调用模型")

    result = baseline.run_baseline(
        [case], model="fake", limit=1, client_factory=exploding_factory
    )
    row = result["results"][0]
    assert row["document_check_mode"] == "baseline"
    assert row["actual_entities"] == []
    assert result["metrics"]["document_leak_check_mode"] == "baseline"
