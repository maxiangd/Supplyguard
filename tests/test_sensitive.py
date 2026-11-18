"""敏感信息测试：脱敏规则与长期记忆落库。"""

from app.services.sensitive import redact_sensitive


def test_redact_sensitive_patterns():
    text = (
        "key sk-abcdefghijklmnop1234，电话 13800138000，"
        "身份证 110101199001011234，密码: abc123"
    )
    redacted, hits = redact_sensitive(text)
    assert "sk-abcdefghijklmnop1234" not in redacted
    assert "13800138000" not in redacted
    assert "110101199001011234" not in redacted
    assert "abc123" not in redacted
    assert set(hits) == {"api_key", "phone", "id_card", "password"}
    assert redacted.count("[敏感信息]") == 4


def test_redact_keeps_normal_text():
    text = "碳酸锂价格回落后，Q4 采购策略需要调整。"
    assert redact_sensitive(text) == (text, [])


def test_long_term_memory_is_redacted(client, auth_headers):
    resp = client.post(
        "/api/memory/long-term",
        headers=auth_headers("procurement"),
        json={
            "category": "note",
            "title": "联系 13800138000",
            "content": "供应商密钥 sk-abcdefghijklmnop1234",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "13800138000" not in body["title"]
    assert "sk-abcdefghijklmnop1234" not in body["content"]
    assert "[敏感信息]" in body["title"]
    assert "[敏感信息]" in body["content"]
