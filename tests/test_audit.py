"""审计导出测试：CSV 转义、BOM 与管理员权限。"""

from app.db.database import get_database_session
from app.db.tables import AuditLog
from app.services.audit import CSV_HEADER, escape_csv_cell


def _session(app):
    """取出测试夹具里被覆盖的数据库会话。"""
    return next(app.dependency_overrides[get_database_session]())


def test_escape_csv_cell_blocks_formulas():
    for value in ("=1+1", "+SUM(A1)", "-2+3", "@cmd", "\tfoo"):
        assert escape_csv_cell(value) == f"'{value}"
    assert escape_csv_cell("\rfoo") == "\"'\rfoo\""


def test_escape_csv_cell_quotes_separators():
    assert escape_csv_cell('a"b') == '"a""b"'
    assert escape_csv_cell("a,b") == '"a,b"'
    assert escape_csv_cell("a\nb") == '"a\nb"'
    assert escape_csv_cell(None) == ""


def test_export_requires_admin(client, auth_headers):
    resp = client.get("/api/audit/export", headers=auth_headers("procurement"))
    assert resp.status_code == 403


def test_export_writes_safe_csv(client, app, auth_headers):
    session = _session(app)
    session.add(
        AuditLog(
            actor="=cmd|calc",
            actor_role="admin",
            action="approval.approve",
            target_type="approval",
            target_id="a-1",
            detail='{"note":"a,b"}',
        )
    )
    session.commit()

    resp = client.get("/api/audit/export", headers=auth_headers("admin"))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment; filename=\"audit_logs_" in resp.headers["content-disposition"]
    assert resp.content.startswith(b"\xef\xbb\xbf")

    text = resp.content.decode("utf-8-sig")
    assert text.splitlines()[0] == ",".join(CSV_HEADER)
    assert "'=cmd|calc" in text
    assert '"a,b"' in text
