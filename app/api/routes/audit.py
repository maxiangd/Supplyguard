"""审计路由：审计日志查询、CSV 导出与清空（仅管理员）。"""

import json
from datetime import date

from fastapi import APIRouter, Depends, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.dependencies import require_roles
from app.db.database import get_database_session
from app.db.tables import User
from app.models.api_models import AuditLogResponse
from app.services.audit import build_audit_csv, list_audit_logs

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("", response_model=list[AuditLogResponse])
def list_logs(
    limit: int = 100,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_database_session),
) -> list[AuditLogResponse]:
    """按时间倒序返回最近的审计记录。"""
    return [
        AuditLogResponse(
            audit_id=log.audit_id,
            actor=log.actor,
            actor_role=log.actor_role,
            action=log.action,
            target_type=log.target_type,
            target_id=log.target_id,
            detail=json.loads(log.detail) if log.detail else None,
            created_at=log.created_at,
        )
        for log in list_audit_logs(db, limit)
    ]


@router.get("/export")
def export_logs(
    limit: int = 1000,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_database_session),
) -> Response:
    """导出最近的审计记录为 CSV（带 BOM，已做公式注入转义）。"""
    csv_text = build_audit_csv(list_audit_logs(db, limit))
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="audit_logs_{date.today():%Y%m%d}.csv"'
            )
        },
    )


@router.delete("")
def clear_logs(
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_database_session),
) -> dict:
    """清空全部审计日志（演示系统管理用）。"""
    count = db.execute(text("SELECT count(1) FROM audit_logs")).scalar() or 0
    db.execute(text("DELETE FROM audit_logs"))
    db.commit()
    return {"status": "ok", "deleted": count}