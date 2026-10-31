"""运维核对入口；默认只读，补传必须显式指定事件、目标及旧状态版本。"""
import argparse
import asyncio
from datetime import UTC, datetime
import json
import re

from sqlalchemy import select, update

from eic.core.database import SessionLocal, engine
from eic.core.ids import new_id
from eic.models import LangfuseDelivery, ObservabilityEvent, Resource


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def execute(args):
    if re.fullmatch(r"[0-9a-f]{64}", args.destination) is None:
        raise ValueError("destination 必须是投递目标摘要")
    async with SessionLocal() as session:
        if args.command == "list":
            rows = await session.scalars(select(LangfuseDelivery).where(
                LangfuseDelivery.destination == args.destination,
            ).order_by(LangfuseDelivery.updated_at, LangfuseDelivery.event_key).limit(args.limit))
            return [{"eventKey": row.event_key, "destination": row.destination, "status": row.status,
                     "exportKey": row.export_key, "attempts": row.attempts,
                     "leaseUntil": utc(row.lease_until).isoformat() if row.lease_until else None,
                     "updatedAt": utc(row.updated_at).isoformat()} for row in rows]
        if (not args.actor.strip() or not args.reason.strip() or not args.evidence.strip()
                or max(len(args.actor), len(args.reason), len(args.evidence)) > 1000):
            raise ValueError("必须提供有界的操作者、原因及核对证据定位；勿填正文或凭据")
        expected = datetime.fromisoformat(args.expected_updated_at)
        if expected.tzinfo is None:
            raise ValueError("expected-updated-at 必须包含时区")
        if args.decision == "retry_uncertain" and not args.allow_duplicate_risk:
            raise ValueError("未知结果补传必须明确接受重复投递风险")
        # 与其他业务遵循 allocator -> 资源行锁顺序；审计与条件更新同事务提交。
        review_id = await new_id(session)
        row = await session.get(LangfuseDelivery, (args.event_key, args.destination), with_for_update=True)
        if (row is None or row.status != args.expected_status or utc(row.updated_at) != utc(expected)
                or row.status not in {"unconfirmed", "rejected", "missing"}):
            raise ValueError("投递记录已变化或不允许核对；重新只读盘点后再操作")
        target = "acknowledged" if args.decision == "confirm_received" else "pending"
        event = await session.get(ObservabilityEvent, args.event_key)
        if event is None:
            raise ValueError("本地原事件缺失，不能补传或确认；先恢复原始事件")
        now = datetime.now(UTC)
        changed = await session.execute(update(LangfuseDelivery).where(
            LangfuseDelivery.event_key == row.event_key, LangfuseDelivery.destination == row.destination,
            LangfuseDelivery.status == args.expected_status, LangfuseDelivery.updated_at == row.updated_at
        ).values(status=target, owner_token=None, lease_until=None, next_attempt_at=now, updated_at=now))
        if changed.rowcount != 1:
            raise ValueError("投递状态并发变化，核对未应用")
        session.add(Resource(id=review_id, tenant_id=event.tenant_id, owner_id=None,
            resource_type="langfuseDeliveryReviews", name="Langfuse 人工投递核对", data={
                "eventKey": args.event_key, "destination": args.destination,
                "before": args.expected_status, "after": target, "decision": args.decision,
                "expectedUpdatedAt": expected.isoformat(), "attempts": row.attempts,
                "actor": args.actor, "reason": args.reason, "evidence": args.evidence,
                "duplicateRiskAccepted": bool(args.allow_duplicate_risk), "reviewedAt": now.isoformat(),
            }))
        await session.commit()
        return {"reviewId": str(review_id), "eventKey": args.event_key, "status": target,
                "networkRequestPerformed": False, "attemptsReset": False}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--destination", required=True)
    listing.add_argument("--limit", type=int, choices=range(1, 101), default=50)
    resolve = commands.add_parser("resolve")
    for name in ("destination", "event-key", "expected-updated-at", "actor", "reason", "evidence"):
        resolve.add_argument("--" + name, required=True)
    resolve.add_argument("--expected-status", choices=["unconfirmed", "rejected", "missing"], required=True)
    resolve.add_argument("--decision", choices=["confirm_received", "confirm_not_received", "retry_uncertain"], required=True)
    resolve.add_argument("--allow-duplicate-risk", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(await execute(args), ensure_ascii=False))
    except (ValueError, KeyError) as exc:
        parser.exit(2, str(exc) + "\n")
    except Exception:
        parser.exit(2, "核对未确认完成，请先只读检查投递及审计记录，不要直接重复补传。\n")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
