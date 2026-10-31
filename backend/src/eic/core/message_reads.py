"""个人阅读证据独立于消息正文及历史共享 isRead。"""
from sqlalchemy import select

from eic.models import MessageReadReceipt


def read_fields(data: dict, read_at=None) -> dict:
    value = data.get("isRead")
    legacy = value is True or value == 1 or (isinstance(value, str) and value.lower() in {"true", "1"})
    return {
        "isRead": read_at is not None,
        "readState": "read" if read_at is not None else "unknown" if legacy else "unread",
        "readAt": read_at.isoformat() if read_at is not None else None,
        "legacyReadUnattributed": legacy and read_at is None,
    }


async def receipts(session, tenant_id: int, user_id: int, message_ids: list[int]) -> dict:
    if not message_ids:
        return {}
    rows = await session.execute(select(
        MessageReadReceipt.message_id, MessageReadReceipt.read_at,
    ).where(
        MessageReadReceipt.tenant_id == tenant_id,
        MessageReadReceipt.user_id == user_id,
        MessageReadReceipt.message_id.in_(message_ids),
    ))
    return dict(rows.all())
