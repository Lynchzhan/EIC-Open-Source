"""真实事务回归：重试替换单独留痕，不能覆盖原始失败运行。"""
from datetime import UTC, datetime, timedelta
from sqlalchemy import select

from eic.core import observability as obs
from eic.core.langfuse_otlp import build_completed_spans
from eic.models import KnowledgeBase, KnowledgeDocument, KnowledgeIngestionRun, ObservabilityEvent
from test_fv2_observability_collection import collecting


async def test_failed_ingestion_and_retry_supersession_remain_distinct(monkeypatch, tmp_path):
    async with collecting(monkeypatch, tmp_path) as (sessions, collector):
        started = datetime.now(UTC) - timedelta(seconds=5)
        finished = started + timedelta(seconds=1)
        async with sessions() as session:
            session.add(KnowledgeBase(id=601, tenant_id=1, name="fixture"))
            await session.flush()
            session.add(KnowledgeDocument(id=602, tenant_id=1, knowledge_base_id=601,
                                          source_name="fixture.txt", content="fixture"))
            await session.flush()
            session.add(KnowledgeIngestionRun(id=603, tenant_id=1, document_id=602, status="failed",
                                             started_at=started, finished_at=finished))
            await session.commit()
        await collector.flush()
        async with sessions() as session:
            run = await session.get(KnowledgeIngestionRun, 603)
            run.status = "superseded"
            await session.commit()
        await collector.flush()
        async with sessions() as session:
            rows = list(await session.scalars(select(ObservabilityEvent).where(
                ObservabilityEvent.source == "knowledge_ingestion_runs")))
            assert {row.status for row in rows} == {"failed", "superseded"}
            failure = next(row for row in rows if row.status == "failed")
            receipt = next(row for row in rows if row.status == "superseded")
            assert failure.duration_ms == 1000 and receipt.duration_ms is None
            assert receipt.occurred_at.replace(tzinfo=UTC) > finished
            first = build_completed_spans(failure, environment="test", namespace="fixture")[0]["body"]
            second = build_completed_spans(receipt, environment="test", namespace="fixture")[0]["body"]
            assert first["spanId"] != second["spanId"]
            assert second["parentSpanId"] == first["spanId"]
            assert second["startTimeUnixNano"] == second["endTimeUnixNano"]
