"""问答档案测试：PostgreSQL 永久保存、会话聚合、用户隔离与删除。"""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.tables import ConversationTurn
from app.services import conversation

FINANCE = SimpleNamespace(username="catl_finance", role="finance")
PROCUREMENT = SimpleNamespace(username="catl_procurement", role="procurement")


@pytest.fixture
def conv_db():
    """只建 conversation_turns 表的内存数据库。"""
    engine = create_engine("sqlite:///:memory:")
    ConversationTurn.__table__.create(bind=engine)
    session_factory = sessionmaker(bind=engine)
    db = session_factory()
    yield db
    db.close()


def test_save_and_list_turns(conv_db):
    conversation.save_turn(
        conv_db,
        FINANCE,
        session_id="s1",
        question="恩捷股份的风险？",
        answer="单一供应商依赖。",
        resolved_question="恩捷股份 风险？",
        entities=[{"id": "supplier-enjie", "name": "恩捷股份", "type": "supplier"}],
        risk_level="HIGH",
        risk_reason="单一供应",
        approval_id="R-1",
        refs=[{"filename": "周报.pdf", "chunk_index": 2}],
    )
    conversation.save_turn(
        conv_db,
        FINANCE,
        session_id="s1",
        question="那成本影响呢？",
        answer="成本上升 5%。",
    )
    conv_db.commit()

    turns = conversation.list_turns(conv_db, FINANCE, "s1")

    assert [turn["question"] for turn in turns] == [
        "恩捷股份的风险？",
        "那成本影响呢？",
    ]
    first = turns[0]
    assert first["entity_names"] == ["恩捷股份"]
    assert first["risk_level"] == "HIGH"
    assert first["approval_id"] == "R-1"
    assert first["refs"][0]["filename"] == "周报.pdf"
    # 没有追问补全时，resolved_question 回落为原始问题
    assert turns[1]["resolved_question"] == "那成本影响呢？"


def test_list_sessions_groups_by_session(conv_db):
    conversation.save_turn(
        conv_db, FINANCE, session_id="s1", question="问题一", answer="答案一"
    )
    conversation.save_turn(
        conv_db, FINANCE, session_id="s1", question="问题二", answer="答案二"
    )
    conversation.save_turn(
        conv_db, FINANCE, session_id="s2", question="另一个会话", answer="答案"
    )
    conv_db.commit()

    sessions = conversation.list_sessions(conv_db, FINANCE)
    by_id = {item["session_id"]: item for item in sessions}

    assert set(by_id) == {"s1", "s2"}
    assert by_id["s1"]["turns"] == 2
    assert by_id["s1"]["last_question"] == "问题二"


def test_conversation_is_user_scoped(conv_db):
    conversation.save_turn(
        conv_db, PROCUREMENT, session_id="s9", question="采购问题", answer="答案"
    )
    conv_db.commit()

    assert conversation.list_sessions(conv_db, FINANCE) == []
    assert conversation.list_turns(conv_db, FINANCE, "s9") == []


def test_delete_session_only_removes_own(conv_db):
    conversation.save_turn(
        conv_db, FINANCE, session_id="s1", question="问题", answer="答案"
    )
    conversation.save_turn(
        conv_db, PROCUREMENT, session_id="s1", question="别人的问题", answer="答案"
    )
    conv_db.commit()

    deleted = conversation.delete_session(conv_db, FINANCE, "s1")
    conv_db.commit()

    assert deleted == 1
    assert conversation.list_turns(conv_db, FINANCE, "s1") == []
    assert len(conversation.list_turns(conv_db, PROCUREMENT, "s1")) == 1


def test_record_turn_writes_redis_and_postgres(conv_db, monkeypatch):
    """问答收尾必须同时写两处：Redis 供追问，PostgreSQL 作永久档案。"""
    from app.services import chat

    captured = {}

    def fake_add_turn(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(chat, "add_turn", fake_add_turn)

    chat._record_turn(
        conv_db,
        FINANCE,
        "s1",
        "恩捷股份的风险？",
        "单一供应商依赖。",
        entities=[{"id": "supplier-enjie", "name": "恩捷股份", "type": "supplier"}],
        risk_level="HIGH",
        refs=[{"filename": "周报.pdf", "chunk_index": 2}],
    )

    assert captured["args"][0] == FINANCE.username
    assert captured["kwargs"]["session_id"] == "s1"

    turns = conversation.list_turns(conv_db, FINANCE, "s1")
    assert len(turns) == 1
    assert turns[0]["risk_level"] == "HIGH"
    assert turns[0]["entity_names"] == ["恩捷股份"]


def test_backfill_from_redis_is_idempotent(conv_db, monkeypatch):
    """上线前已存在的 Redis 会话可以一次性补录，重复执行不会重复写入。"""
    from app.services import memory
    from tests.test_memory import FakeRedis

    client = FakeRedis()
    monkeypatch.setattr(memory, "_redis", lambda: client)
    memory.add_turn(
        "catl_finance",
        "finance",
        "旧问题",
        "旧回答",
        session_id="s-old",
        entities=[{"id": "e1", "name": "E1"}],
        risk_level="HIGH",
    )

    assert conversation.backfill_from_redis(conv_db) == 1
    conv_db.commit()
    assert conversation.backfill_from_redis(conv_db) == 0

    turns = conversation.list_turns(conv_db, FINANCE, "s-old")
    assert turns[0]["question"] == "旧问题"
    assert turns[0]["entity_names"] == ["E1"]


def test_prepare_memory_merges_archived_turns(conv_db, monkeypatch):
    """Redis 过期后，仍能从永久档案恢复多轮上下文。"""
    from app.services import chat, followup, memory
    from tests.test_memory import FakeRedis

    monkeypatch.setattr(memory, "_redis", lambda: FakeRedis())
    monkeypatch.setattr(followup, "match_entities", lambda question, role: [])

    conversation.save_turn(
        conv_db,
        FINANCE,
        session_id="s1",
        question="上一轮问题",
        answer="上一轮回答",
        entities=[{"id": "e1", "name": "E1", "type": "supplier"}],
        risk_level="HIGH",
    )
    conv_db.commit()

    history, followup_ctx, long_term = chat._prepare_memory(
        conv_db, FINANCE, "那它呢？", "finance", "s1"
    )

    assert history[-1]["question"] == "上一轮问题"
    assert followup_ctx.carried_entity_ids == ["e1"]
    assert long_term == ""


def test_conversation_api_endpoints(client, auth_headers):
    headers = auth_headers("finance")

    sessions = client.get("/api/memory/conversations", headers=headers)
    assert sessions.status_code == 200
    assert sessions.json()["total"] == 0

    detail = client.get("/api/memory/conversations/not-exist", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["count"] == 0

    deleted = client.delete("/api/memory/conversations/not-exist", headers=headers)
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] == 0
