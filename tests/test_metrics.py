"""指标测试：/metrics 可抓取，请求与复核拦截会累加。"""

from types import SimpleNamespace

from app.core.metrics import RBAC_DROPS
from app.services import chat
from app.services.review import review_chunks


def test_metrics_endpoint_exposes_http_counter(client):
    client.get("/api/health/live")
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "supplyguard_http_requests_total" in resp.text
    assert 'path="/api/health/live"' in resp.text


def test_review_drops_increment_counter():
    before = RBAC_DROPS.labels("chunks")._value.get()
    passed, filtered = review_chunks(
        [
            {"document_id": "d1", "allowed_roles": ["finance"]},
            {"document_id": "d2", "allowed_roles": ["procurement"]},
        ],
        "procurement",
    )
    assert [chunk["document_id"] for chunk in passed] == ["d2"]
    assert len(filtered) == 1
    assert RBAC_DROPS.labels("chunks")._value.get() == before + 1


def test_create_completion_accepts_model_in_kwargs(monkeypatch):
    """流式与非流式都把 model 放在关键字参数里，避免重复传参。"""
    captured = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(choices=[])

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions())
    )
    monkeypatch.setattr(chat, "get_chat_client", lambda _model: fake_client)

    chat._create_completion(model="deepseek-chat", messages=[], stream=True)
    assert captured["model"] == "deepseek-chat"
