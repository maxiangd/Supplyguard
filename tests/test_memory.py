"""记忆与上下文管理测试：短期会话、追问解析、长期记忆、历史风险。

Redis 用内存假实现，避免测试依赖本地 Redis；
PostgreSQL 用内存 SQLite，只建 long_term_memories 一张表。
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.configuration import settings
from app.db.tables import LongTermMemory
from app.services import followup, long_term, memory, summarize


# ---------------------------------------------------------------------------
# 内存版 Redis
# ---------------------------------------------------------------------------


class FakePipeline:
    """只实现本模块用到的三个命令，按入队顺序执行。"""

    def __init__(self, redis_client: "FakeRedis"):
        self.redis_client = redis_client
        self.operations: list[tuple] = []

    def rpush(self, key, value):
        self.operations.append(("rpush", key, value))
        return self

    def ltrim(self, key, start, end):
        self.operations.append(("ltrim", key, start, end))
        return self

    def expire(self, key, seconds):
        self.operations.append(("expire", key, seconds))
        return self

    def execute(self):
        for operation in self.operations:
            method = getattr(self.redis_client, operation[0])
            method(*operation[1:])
        self.operations = []


class FakeRedis:
    """满足 memory.py 所需接口的极简 Redis。"""

    def __init__(self):
        self.lists: dict[str, list[str]] = {}
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    def ltrim(self, key, start, end):
        items = self.lists.get(key, [])
        length = len(items)
        start = max(length + start, 0) if start < 0 else start
        end = length + end if end < 0 else end
        self.lists[key] = items[start : end + 1] if end >= start else []
        return True

    def lrange(self, key, start, end):
        items = self.lists.get(key, [])
        length = len(items)
        start = max(length + start, 0) if start < 0 else start
        end = length + end if end < 0 else end
        return items[start : end + 1] if end >= start else []

    def expire(self, key, seconds):
        self.ttls[key] = seconds
        return True

    def ttl(self, key):
        return self.ttls.get(key, -1)

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value
        return True

    def delete(self, key):
        self.lists.pop(key, None)
        self.values.pop(key, None)
        return 1

    def scan_iter(self, match=None, count=None):
        keys = list(self.lists.keys())
        if match is None:
            return iter(keys)
        prefix = match.rstrip("*")
        return iter([key for key in keys if key.startswith(prefix)])

    def pipeline(self):
        return FakePipeline(self)


@pytest.fixture
def fake_redis(monkeypatch):
    client = FakeRedis()
    monkeypatch.setattr(memory, "_redis", lambda: client)
    return client


# ---------------------------------------------------------------------------
# 短期会话记忆
# ---------------------------------------------------------------------------


def test_add_and_read_turns(fake_redis):
    memory.add_turn(
        "catl_finance",
        "finance",
        "宜春停产有什么影响？",
        "影响 Q4 交付。",
        session_id="chat-a",
        entities=[{"id": "event-yichun", "name": "宜春停产"}],
        risk_level="HIGH",
        approval_id="R-1",
    )
    memory.add_turn(
        "catl_finance", "finance", "那替代港口呢？", "可走上海港。", session_id="chat-a"
    )

    history = memory.get_session_history("catl_finance", "chat-a")

    assert [turn["question"] for turn in history] == [
        "宜春停产有什么影响？",
        "那替代港口呢？",
    ]
    assert history[0]["entity_ids"] == ["event-yichun"]
    assert history[0]["entity_names"] == ["宜春停产"]
    assert history[0]["risk_level"] == "HIGH"
    assert history[0]["approval_id"] == "R-1"
    assert memory.get_latest_turn("catl_finance", "chat-a")["question"] == "那替代港口呢？"


def test_sessions_are_isolated(fake_redis):
    memory.add_turn("catl_finance", "finance", "会话 A 的问题", "答案", session_id="a")
    memory.add_turn("catl_finance", "finance", "会话 B 的问题", "答案", session_id="b")

    assert len(memory.get_session_history("catl_finance", "a")) == 1
    assert len(memory.get_session_history("catl_finance", "b")) == 1
    assert memory.get_session_history("catl_finance", "c") == []


def test_clear_only_target_session(fake_redis):
    memory.add_turn("catl_finance", "finance", "问题 A", "答案", session_id="a")
    memory.add_turn("catl_finance", "finance", "问题 B", "答案", session_id="b")

    assert memory.clear_session("catl_finance", "a") is True
    assert memory.get_session_history("catl_finance", "a") == []
    assert len(memory.get_session_history("catl_finance", "b")) == 1


def test_turn_limit_is_enforced(fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "memory_max_turns", 2)
    for index in range(5):
        memory.add_turn("u", "finance", f"q{index}", "a", session_id="s")

    history = memory.get_session_history("u", "s")

    assert [turn["question"] for turn in history] == ["q3", "q4"]


def test_legacy_key_is_readable(fake_redis):
    fake_redis.values["supplyguard:session:u"] = json.dumps(
        [{"role": "finance", "question": "旧问题", "answer": "旧回答", "timestamp": 1}],
        ensure_ascii=False,
    )

    history = memory.get_session_history("u", "default")

    assert history[0]["question"] == "旧问题"


def test_invalid_session_id_falls_back_to_default(fake_redis):
    assert memory.normalize_session_id("../etc/passwd") == "default"
    assert memory.normalize_session_id("ok-id_123") == "ok-id_123"
    assert memory.normalize_session_id(None) == "default"


def test_build_memory_context_contains_entities_and_risk(fake_redis):
    memory.add_turn(
        "u",
        "finance",
        "宜春停产有什么影响？",
        "影响 Q4 交付。",
        session_id="s",
        entities=[{"id": "event-yichun", "name": "宜春停产"}],
        risk_level="HIGH",
        approval_id="R-1",
    )

    context = memory.build_memory_context(memory.get_session_history("u", "s"))

    assert "宜春停产" in context
    assert "HIGH" in context
    assert "R-1" in context


def test_list_sessions_summarizes_and_isolates(fake_redis):
    memory.add_turn(
        "catl_finance",
        "finance",
        "问题一",
        "答案一",
        session_id="a",
        entities=[{"id": "e1", "name": "E1"}],
    )
    memory.add_turn("catl_finance", "finance", "问题二", "答案二", session_id="a")
    memory.add_turn("catl_finance", "finance", "另一个会话", "答案", session_id="b")
    memory.add_turn("catl_procurement", "procurement", "别人的会话", "答案", session_id="c")

    sessions = memory.list_sessions("catl_finance")

    assert {item["session_id"] for item in sessions} == {"a", "b"}
    session_a = next(item for item in sessions if item["session_id"] == "a")
    assert session_a["turns"] == 2
    assert session_a["last_question"] == "问题二"


# ---------------------------------------------------------------------------
# 多轮追问解析
# ---------------------------------------------------------------------------


def test_followup_inherits_previous_entities(monkeypatch):
    monkeypatch.setattr(followup, "match_entities", lambda question, role: [])
    history = [
        {
            "question": "宜春停产对宁德基地有什么影响？",
            "entity_ids": ["event-yichun"],
            "entity_names": ["宜春停产"],
        }
    ]

    context = followup.resolve_followup("那替代港口呢？", "procurement", history)

    assert context.is_followup is True
    assert context.carried_entity_ids == ["event-yichun"]
    assert context.carried_entity_names == ["宜春停产"]
    assert context.resolved_question.startswith("宜春停产")


def test_followup_does_not_hijack_new_entity(monkeypatch):
    monkeypatch.setattr(
        followup, "match_entities", lambda question, role: [{"id": "event-new"}]
    )
    history = [
        {
            "question": "宜春停产",
            "entity_ids": ["event-yichun"],
            "entity_names": ["宜春停产"],
        }
    ]

    context = followup.resolve_followup("印尼镍政策呢？", "procurement", history)

    assert context.is_followup is False
    assert context.resolved_question == "印尼镍政策呢？"
    assert context.carried_entity_ids == []


def test_followup_without_history_is_plain_question():
    context = followup.resolve_followup("那它呢？", "finance", [])

    assert context.is_followup is False
    assert context.resolved_question == "那它呢？"


def test_long_standalone_question_skips_entity_lookup(monkeypatch):
    def explode(question, role):
        raise AssertionError("独立长问题不应触发实体匹配")

    monkeypatch.setattr(followup, "match_entities", explode)
    history = [{"question": "旧", "entity_ids": ["e"], "entity_names": ["E"]}]

    context = followup.resolve_followup(
        "红海绕行对欧洲客户交付路径有什么影响？", "logistics", history
    )

    assert context.is_followup is False


# ---------------------------------------------------------------------------
# 长期记忆
# ---------------------------------------------------------------------------


@pytest.fixture
def memory_db():
    """只建 long_term_memories 表的内存数据库。"""
    engine = create_engine("sqlite:///:memory:")
    LongTermMemory.__table__.create(bind=engine)
    session_factory = sessionmaker(bind=engine)
    db = session_factory()
    yield db
    db.close()


def finance_user():
    return SimpleNamespace(username="catl_finance", role="finance")


def test_load_memory_context_includes_preference_and_related_risk(memory_db):
    user = finance_user()
    long_term.save_memory(
        memory_db,
        username=user.username,
        role=user.role,
        category=long_term.CATEGORY_PREFERENCE,
        title="回答风格",
        content="优先给结论，再列证据。",
    )
    long_term.save_risk_memory(
        memory_db,
        user,
        question="恩捷股份隔膜供应风险",
        answer="单一供应商依赖，建议备份。",
        risk_level="HIGH",
        risk_reason="隔膜单一供应",
        entities=[{"id": "supplier-enjie", "name": "恩捷股份"}],
    )
    memory_db.commit()

    context = long_term.load_memory_context(
        memory_db, user, question="恩捷股份怎么样", entity_ids=["supplier-enjie"]
    )

    assert "回答风格" in context
    assert "[HIGH]" in context
    assert "恩捷股份" in context


def test_load_memory_context_includes_manual_note(memory_db):
    """问答页「记住这条」存成 note，也必须进入后续提示词。"""
    user = finance_user()
    long_term.save_memory(
        memory_db,
        username=user.username,
        role=user.role,
        category=long_term.CATEGORY_NOTE,
        title="恩捷股份结论",
        content="隔膜单一供应，建议提前锁量。",
    )
    memory_db.commit()

    context = long_term.load_memory_context(
        memory_db, user, question="恩捷股份怎么样", entity_ids=[]
    )

    assert "恩捷股份结论" in context


def test_load_memory_context_is_user_scoped(memory_db):
    user = finance_user()
    long_term.save_memory(
        memory_db,
        username="catl_procurement",
        role="procurement",
        category=long_term.CATEGORY_PREFERENCE,
        title="别人的偏好",
        content="不应出现",
    )
    memory_db.commit()

    context = long_term.load_memory_context(memory_db, user)

    assert context == ""


def test_save_risk_memory_deduplicates_within_window(memory_db):
    user = finance_user()
    entities = [{"id": "supplier-enjie", "name": "恩捷股份"}]

    first = long_term.save_risk_memory(
        memory_db, user, "问题一", "回答一", "HIGH", "风险一", entities
    )
    second = long_term.save_risk_memory(
        memory_db, user, "问题二", "回答二", "HIGH", "风险二", entities
    )

    count = memory_db.query(LongTermMemory).count()
    assert count == 1
    assert first.memory_id == second.memory_id


def test_save_risk_memory_ignores_low_risk(memory_db):
    result = long_term.save_risk_memory(
        memory_db, finance_user(), "问题", "回答", "LOW", "无风险", []
    )

    assert result is None
    assert memory_db.query(LongTermMemory).count() == 0


def test_list_risk_memories_visibility_and_filters(memory_db):
    finance = finance_user()
    procurement = SimpleNamespace(username="catl_procurement", role="procurement")
    admin = SimpleNamespace(username="catl_admin", role="admin")

    long_term.save_risk_memory(
        memory_db,
        finance,
        "恩捷风险",
        "回答",
        "HIGH",
        "单一供应",
        [{"id": "supplier-enjie", "name": "恩捷股份"}],
    )
    long_term.save_risk_memory(
        memory_db,
        procurement,
        "红海风险",
        "回答",
        "MEDIUM",
        "航线延误",
        [{"id": "route-redsea", "name": "红海航线"}],
    )
    memory_db.commit()

    assert len(long_term.list_risk_memories(memory_db, finance)) == 1
    assert len(long_term.list_risk_memories(memory_db, admin)) == 2
    assert (
        len(
            long_term.list_risk_memories(
                memory_db, admin, entity_id="supplier-enjie"
            )
        )
        == 1
    )
    assert len(long_term.list_risk_memories(memory_db, admin, level="MEDIUM")) == 1


# ---------------------------------------------------------------------------
# 长期记忆摘要
# ---------------------------------------------------------------------------


def test_summarize_text_strips_markdown_and_citations():
    text = "## 结论\n\n**Q4 应以控库存为主**，不宜囤货 [G1][G2]。具体来看，价格回落会传导成本 [1]。"

    summary = summarize.summarize_text(text, limit=80)

    assert "Q4 应以控库存为主" in summary
    assert "[G1]" not in summary
    assert "**" not in summary
    assert len(summary) <= 80


def test_summarize_answer_uses_llm(monkeypatch):
    monkeypatch.setattr(settings, "memory_summarize_with_llm", True)
    monkeypatch.setattr(summarize, "available_models", lambda: ["deepseek-chat"])
    monkeypatch.setattr(summarize, "default_model", lambda: "deepseek-chat")

    class Completions:
        def create(self, **kwargs):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="Q4 应以控库存为主。")
                    )
                ]
            )

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=Completions())
    )
    monkeypatch.setattr(
        summarize, "get_chat_client", lambda model, **kwargs: fake_client
    )

    result = summarize.summarize_answer("Q4？", "很长的回答" * 100, limit=120)

    assert result == "Q4 应以控库存为主。"


def test_summarize_answer_falls_back_without_llm(monkeypatch):
    monkeypatch.setattr(settings, "memory_summarize_with_llm", False)

    result = summarize.summarize_answer("Q4？", "结论：控库存。" * 100, limit=80)

    assert "控库存" in result
    assert len(result) <= 80


def test_save_risk_memory_is_concise(memory_db):
    user = finance_user()
    answer = "**结论：Q4 应以控库存为主，不宜囤货。** " + "具体细节。" * 200

    memory = long_term.save_risk_memory(
        memory_db,
        user,
        question="Q4 采购策略？",
        answer=answer,
        risk_level="HIGH",
        risk_reason="成本与库存风险",
        entities=[{"id": "event-x", "name": "碳酸锂价格回落"}],
    )

    assert memory.content != answer
    assert len(memory.content) <= 200
    assert memory.title.startswith("[HIGH]")
    assert "碳酸锂价格回落" in memory.title


def test_save_chat_note_is_concise(memory_db):
    user = finance_user()
    answer = "结论：可以先小批量采购。" + "详细解释。" * 200

    memory = long_term.save_chat_note(
        memory_db, user, "采购怎么做？", answer, session_id="s1"
    )

    assert memory.category == long_term.CATEGORY_NOTE
    assert len(memory.content) <= 200
    assert memory.title == "采购怎么做？"


def test_compact_memories_shrinks_existing_rows(memory_db):
    user = finance_user()
    memory = long_term.save_memory(
        memory_db,
        username=user.username,
        role=user.role,
        category=long_term.CATEGORY_NOTE,
        title="旧笔记",
        content="结论：应控库存。" + "细节" * 200,
    )
    memory_db.commit()

    updated = long_term.compact_memories(memory_db)
    memory_db.commit()

    assert updated == 1
    assert len(memory.content) <= 200


def test_list_memories_includes_risk(memory_db):
    user = finance_user()
    long_term.save_risk_memory(
        memory_db,
        user,
        "风险问题",
        "风险回答",
        "HIGH",
        "单一供应",
        [{"id": "supplier-enjie", "name": "恩捷股份"}],
    )
    memory_db.commit()

    items = long_term.list_memories(memory_db, user)

    assert any(item.category == long_term.CATEGORY_RISK for item in items)


# ---------------------------------------------------------------------------
# 接口测试
# ---------------------------------------------------------------------------


def test_session_api_respects_session_id(client, auth_headers, fake_redis):
    memory.add_turn("catl_finance", "finance", "会话 A", "答案", session_id="chat-a")
    headers = auth_headers("finance")

    response = client.get("/api/memory/session?session_id=chat-a", headers=headers)
    assert response.status_code == 200
    assert response.json()["count"] == 1

    other = client.get("/api/memory/session?session_id=chat-b", headers=headers)
    assert other.json()["count"] == 0


def test_sessions_api_lists_all_user_sessions(client, auth_headers, fake_redis):
    memory.add_turn("catl_finance", "finance", "会话 A", "答案", session_id="a")
    memory.add_turn("catl_finance", "finance", "会话 B", "答案", session_id="b")

    response = client.get("/api/memory/sessions", headers=auth_headers("finance"))
    data = response.json()

    assert data["total"] == 2
    assert {item["session_id"] for item in data["sessions"]} == {"a", "b"}

    other = client.get("/api/memory/sessions", headers=auth_headers("procurement"))
    assert other.json()["total"] == 0


def test_remember_api_saves_summary(client, auth_headers):
    headers = auth_headers("finance")

    response = client.post(
        "/api/memory/remember",
        json={
            "question": "Q4 采购策略？",
            "answer": "**结论：应以控库存为主。** " + "细节" * 300,
            "session_id": "s1",
            "refs": [{"filename": "a.pdf", "chunk_index": 0}],
        },
        headers=headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["category"] == "note"
    assert data["title"] == "Q4 采购策略？"
    assert len(data["content"]) <= 200

    items = client.get("/api/memory/long-term", headers=headers).json()["items"]
    assert any(item["memory_id"] == data["memory_id"] for item in items)


def test_risk_memory_api(client, auth_headers):
    evidence = json.dumps(
        {"entity_ids": ["supplier-enjie"], "risk_level": "HIGH"},
        ensure_ascii=False,
    )
    finance_headers = auth_headers("finance")

    created = client.post(
        "/api/memory/long-term",
        json={
            "category": "risk",
            "title": "[HIGH] 隔膜风险",
            "content": "单一供应商依赖",
            "evidence": evidence,
        },
        headers=finance_headers,
    )
    assert created.status_code == 200

    own = client.get(
        "/api/memory/risks?entity_id=supplier-enjie", headers=finance_headers
    )
    assert own.json()["total"] == 1

    other = client.get(
        "/api/memory/risks?entity_id=supplier-enjie",
        headers=auth_headers("procurement"),
    )
    assert other.json()["total"] == 0


# ---------------------------------------------------------------------------
# 工作流接线：追问补全与长期记忆必须真正进入检索和提示词
# ---------------------------------------------------------------------------


def test_retrieve_uses_resolved_question(monkeypatch):
    from app.services import workflow

    captured = {}

    def fake_search(question, role, top_k):
        captured["question"] = question
        return []

    monkeypatch.setattr(workflow, "search_chunks", fake_search)
    workflow.retrieve_node(
        {
            "question": "那它呢？",
            "resolved_question": "宜春停产 那它呢？",
            "role": "procurement",
            "top_k": 4,
        }
    )

    assert captured["question"] == "宜春停产 那它呢？"


def test_graph_analyze_passes_carried_entities(monkeypatch):
    from app.services import workflow

    captured = {}

    def fake_impact(question, entity_id, role, max_hops=3, extra_entity_ids=None):
        captured["extra"] = extra_entity_ids
        captured["question"] = question
        return {"entities": [], "paths": []}

    monkeypatch.setattr(workflow, "impact", fake_impact)
    monkeypatch.setattr(workflow, "overview", lambda role: {"nodes": [], "links": []})

    workflow.graph_analyze_node(
        {
            "question": "那它呢？",
            "resolved_question": "宜春停产 那它呢？",
            "carried_entity_ids": ["event-yichun"],
            "role": "procurement",
        }
    )

    assert captured["extra"] == ["event-yichun"]
    assert captured["question"] == "宜春停产 那它呢？"


def test_build_context_includes_session_summary():
    from app.services import workflow

    result = workflow.build_context_node(
        {
            "question": "继续说一下",
            "session_summary": "更早讨论过宜春锂矿停产与复产时间待定。",
            "review_passed_hits": [],
            "review_passed_paths": [],
            "alt_context": "",
            "infra_context": "",
        }
    )

    content = result["messages"][1]["content"]
    assert "更早对话摘要" in content
    assert "宜春锂矿停产" in content


def test_build_context_includes_long_term_memory():
    from app.services import workflow

    result = workflow.build_context_node(
        {
            "question": "恩捷股份怎么样？",
            "long_term_context": "长期记忆：用户偏好优先给结论。",
            "review_passed_hits": [],
            "review_passed_paths": [],
            "alt_context": "",
            "infra_context": "",
        }
    )

    content = result["messages"][1]["content"]
    assert "长期记忆" in content
    assert "用户偏好优先给结论" in content


# ---------------------------------------------------------------------------
# 多轮追问 Benchmark
# ---------------------------------------------------------------------------

CASES_PATH = Path(__file__).parent / "benchmark" / "cases.json"


def load_benchmark_cases() -> list[dict]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def test_multiturn_cases_inherit_previous_entities():
    from tests.benchmark.ground_truth import expected_paths_for_case

    cases = [c for c in load_benchmark_cases() if c.get("scenario") == "multi_turn"]
    assert cases, "应当存在 multi_turn 用例"

    for case in cases:
        gt = expected_paths_for_case(
            case["question"], case["role"], case.get("context")
        )
        assert gt["inherited_entity_ids"] == case["expected_entities"]
        assert gt["path_count"] >= case["expected_min_paths"]
        assert gt["leaked_node_ids"] == []


def test_multiturn_without_context_returns_nothing():
    """追问问题本身命中不到实体，验证用例确实依赖上下文继承。"""
    from tests.benchmark.ground_truth import expected_paths_for_case

    case = next(c for c in load_benchmark_cases() if c["id"] == "case_027")
    gt = expected_paths_for_case(case["question"], case["role"], None)

    assert gt["inherited_entity_ids"] == []
    assert gt["path_count"] == 0


def test_benchmark_reports_followup_metrics():
    from app.api.routes.benchmark import run_benchmark

    data = run_benchmark()
    metrics = data["metrics"]

    assert metrics["followup_case_total"] == 4
    assert metrics["followup_case_passed"] == 4
    assert metrics["followup_success_rate"] == 1.0
    assert metrics["document_leak_count"] == 0
