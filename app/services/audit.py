"""审计服务：只追加写入审计日志，并提供查询与 CSV 导出。"""

import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.tables import AuditLog

CSV_HEADER = ["时间", "用户", "角色", "动作", "对象", "详情"]


def record_audit(
    db: Session,
    actor: str,
    actor_role: str,
    action: str,
    target_type: str | None,
    target_id: str | None,
    detail: dict,
) -> None:
    """追加一条审计记录，detail 以 JSON 字符串存储。"""
    db.add(
        AuditLog(
            actor=actor,
            actor_role=actor_role,
            action=action,
            target_type=target_type,
            target_id=target_id,
            detail=json.dumps(detail, ensure_ascii=False),
        )
    )
    # 立即落地，保证同一事务内立即可查到（会话默认 autoflush=False）
    db.flush()


def list_audit_logs(db: Session, limit: int) -> list[AuditLog]:
    """按时间倒序返回最近的审计记录。"""
    statement = (
        select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    )
    return list(db.scalars(statement))


def escape_csv_cell(value: object) -> str:
    """把单元格转成安全 CSV：防公式注入并处理引号与换行。"""
    text = "" if value is None else str(value)
    # Excel/WPS 会把 = + - @ 或制表符/回车开头的值当公式，前面加单引号阻断
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        text = f"'{text}"
    if any(char in text for char in (",", '"', "\n", "\r")):
        text = f'"{text.replace(chr(34), chr(34) * 2)}"'
    return text


def build_audit_csv(logs: list[AuditLog]) -> str:
    """生成带 BOM 的审计 CSV 文本，Excel 直接打开不乱码。"""
    lines = [",".join(CSV_HEADER)]
    for log in logs:
        created_at: datetime = log.created_at
        lines.append(
            ",".join(
                escape_csv_cell(value)
                for value in (
                    created_at.isoformat(sep=" ", timespec="seconds"),
                    log.actor,
                    log.actor_role,
                    log.action,
                    log.target_id,
                    log.detail,
                )
            )
        )
    return "﻿" + "\r\n".join(lines) + "\r\n"