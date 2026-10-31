import json
import os
import time
from pathlib import Path

from fastapi.testclient import TestClient

TEST_DB = Path(__file__).with_name("test_eic.db")
os.environ.setdefault("EIC_DATABASE_URL", "sqlite+aiosqlite:///" + TEST_DB.as_posix())
os.environ.setdefault("EIC_BOOTSTRAP_ADMIN_PASSWORD", "TestOnly-Admin-Password-2026-Long")
os.environ.setdefault("EIC_JWT_SECRET", "test-jwt-secret-with-at-least-thirty-two-characters")
os.environ.setdefault("EIC_SECRET_ENCRYPTION_KEY", "test-encryption-secret-with-at-least-thirty-two-characters")

from eic.knowledge.evaluation import aggregate_eval_scores, evaluate_case, hit_at_k, mean_reciprocal_rank, recall_at_k
from eic.main import app, coerce_id


def consume_import(client, response, headers, monkeypatch):
    # 响应确认持久意图已排队；后台 broker 桩不投递，消费仍须显式驱动。
    assert response.json()['queued'] is True
    assert response.json()['jobId'].startswith('eic-outbox:')
    document_id=response.json()['documentId']
    async def consume():
        from sqlalchemy import select
        from eic.core import outbox
        from eic.core.database import SessionLocal
        from eic.models import OutboxEvent
        from eic.models import AIModel, KnowledgeBase, KnowledgeDocument
        from eic.core.ids import new_id
        from test_index_lifecycle import projection
        projection(monkeypatch)
        async with SessionLocal() as session:
            event=await session.scalar(select(OutboxEvent).where(OutboxEvent.entity_id==document_id,
                OutboxEvent.kind=='knowledge-document').order_by(OutboxEvent.id.desc()))
            assert event is not None and event.consume_status=='pending'
            identity,tenant=event.id,event.tenant_id
            document=await session.get(KnowledgeDocument,document_id)
            base=await session.get(KnowledgeBase,document.knowledge_base_id)
            if not base.embedding_model_id:
                model=AIModel(id=await new_id(session),tenant_id=tenant,provider='openai',
                    model_type='embedding',model_name='offline-fixture',endpoint='http://fixture.invalid',
                    enabled=True,embedding_dimensions=2)
                session.add(model)
                await session.flush()
                base.embedding_model_id=model.id
                base.inherits_embedding_model=False
            from datetime import timedelta
            event.available_at=await outbox.now(session)-timedelta(seconds=1)
            await session.commit()
        result=await outbox.consume_event({},identity,tenant)
        assert result.get('status') == 'parsed_pending_embedding',result
        # 本地协议桩仅替代向量外部 I/O，仍执行真实解析、分块、消费及版本激活。
        async with SessionLocal() as session:
            embedding=await session.scalar(select(OutboxEvent).where(OutboxEvent.entity_id==document_id,
                OutboxEvent.kind=='document-embeddings').order_by(OutboxEvent.id.desc()))
            assert embedding is not None
            embedding.available_at=await outbox.now(session)-timedelta(seconds=1)
            embedding_id=embedding.id
            await session.commit()
        assert (await outbox.consume_event({},embedding_id,tenant))['status']=='ready'
    client.portal.call(consume)
    detail=client.get('/api/AIKmss/GetDocument',params={'Id':document_id},headers=headers)
    assert detail.status_code==200
    assert detail.json()['statusKey']=='ready',detail.text
    assert detail.json()['chunkCount']>0


def login(client: TestClient, monkeypatch=None) -> dict[str, str]:
    response = client.post(
        "/api/Authorize/GetToken",
        json={"name": "admin", "passWord": os.environ["EIC_BOOTSTRAP_ADMIN_PASSWORD"], "tenantId": 1000},
    )
    assert response.status_code == 200, response.text
    if monkeypatch is not None:
        from test_index_lifecycle import projection
        from eic.knowledge import ops
        from eic.core import outbox
        from unittest.mock import AsyncMock
        projection(monkeypatch)
        monkeypatch.setattr(outbox,'dispatch_one',AsyncMock(return_value=None))
        monkeypatch.setattr(ops,'_search_dense',AsyncMock(return_value=([],'unavailable')))
        async def seed_default():
            from sqlalchemy import select
            from eic.core.database import SessionLocal
            from eic.core.ids import new_id
            from eic.models import AIModel, TenantModelDefaults, User
            async with SessionLocal() as session:
                user=await session.scalar(select(User).where(User.username=='admin'))
                defaults=await session.get(TenantModelDefaults,user.tenant_id)
                if defaults is None:
                    defaults=TenantModelDefaults(tenant_id=user.tenant_id)
                    session.add(defaults)
                if not defaults.embedding_model_id:
                    model=AIModel(id=await new_id(session),tenant_id=user.tenant_id,provider='openai',
                        model_type='embedding',model_name='offline-fixture',endpoint='http://fixture.invalid',
                        enabled=True,embedding_dimensions=2)
                    session.add(model)
                    await session.flush()
                    defaults.embedding_model_id=model.id
                await session.commit()
        client.portal.call(seed_default)
    return {"Authorization": f"Bearer {response.json()}"}


def error_text(response) -> str:
    try:
        return json.dumps(response.json(), ensure_ascii=False)
    except Exception:
        return response.text


def test_retrieval_metrics_are_deterministic() -> None:
    ranked = ["d1", "d2", "d3"]
    relevant = {"d2", "d4"}
    assert hit_at_k(ranked, relevant, 1) == 0.0
    assert hit_at_k(ranked, relevant, 2) == 1.0
    assert recall_at_k(ranked, relevant, 2) == 0.5
    assert mean_reciprocal_rank(ranked, relevant) == 0.5
    case = evaluate_case(
        [{"chunkId": "c2", "documentId": "d2", "documentName": "faq"}],
        ["d2"],
        ["c2"],
        k=5,
    )
    assert case["hitAtK"] == 1.0
    assert case["recallAtK"] == 1.0
    assert case["mrr"] == 1.0
    summary = aggregate_eval_scores([case, {**case, "hit": False, "hitAtK": 0.0, "recallAtK": 0.0, "mrr": 0.0}])
    assert summary["caseCount"] == 2
    assert summary["hitAtK"] == 0.5
    assert summary["missCount"] == 1


def test_object_file_id_returns_chinese_error() -> None:
    assert coerce_id("[object Object]") is None
    assert coerce_id({"id": "42"}) == 42
    with TestClient(app) as client:
        headers = login(client)
        by_id = client.get("/api/File/GetById", params={"Id": "[object Object]"}, headers=headers)
        assert by_id.status_code in {400, 422}, by_id.text
        assert "\u6587\u4ef6 ID \u65e0\u6548" in error_text(by_id)
        created = client.post("/api/AIKmss/AddEdit", headers=headers, json={"name": f"id-check-{int(time.time()*1000)}"})
        assert created.json() is True
        kb = client.post("/api/AIKmss/GetList", headers=headers, json={"search": "id-check-"}).json()["data"][0]
        imported = client.post(
            "/api/AIKmss/ImportFile",
            params={"Id": kb["id"], "fileId": "[object Object]"},
            headers=headers,
        )
        assert imported.status_code in {400, 422}, imported.text
        assert "\u6587\u4ef6 ID \u65e0\u6548" in error_text(imported)


def test_knowledge_document_eval_and_chinese_skill(monkeypatch) -> None:
    pdf_bytes = (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
        b"4 0 obj<</Length 51>>stream\n"
        b"BT /F1 12 Tf 20 120 Td (refund within seven days) Tj ET\n"
        b"endstream\nendobj\n"
        b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF\n"
    )
    stamp = str(int(time.time() * 1000))
    with TestClient(app) as client:
        headers = login(client,monkeypatch)
        kb_name = f"\u5ba2\u6237\u670d\u52a1\u77e5\u8bc6\u5e93-{stamp}"
        assert client.post(
            "/api/AIKmss/AddEdit",
            headers=headers,
            json={
                "name": kb_name,
                "maxTokensPerParagraph": 180,
                "overlappingTokens": 20,
                "retrievalStrategy": "full",
            },
        ).json() is True
        kb = next(item for item in client.post("/api/AIKmss/GetList", headers=headers, json={}).json()["data"] if item["name"] == kb_name)
        kb_id = kb["id"]

        saved = client.post(
            "/api/AIKmss/SaveRetrieval",
            headers=headers,
            json={"id": kb_id, "retrievalStrategy": "advanced", "retrievalConfig": {"topK": 6, "rrfK": 40}},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["retrievalStrategy"] == "advanced"

        imported_text = client.post(
            "/api/AIKmss/ImportText",
            headers=headers,
            json={"knowledgeBaseId": kb_id, "contentName": "faq", "content": "\u9000\u8d27\u97007\u5929\u5185\u7533\u8bf7\u3002\u5ba2\u670d\u7535\u8bdd10086\u3002"},
        )
        assert imported_text.status_code == 200, imported_text.text
        assert imported_text.json()['status']=='queued_text'
        consume_import(client,imported_text,headers,monkeypatch)
        documents = client.get("/api/AIKmss/GetDocuments", params={"Id": kb_id}, headers=headers)
        assert documents.status_code == 200, documents.text
        rows = documents.json()
        assert any(item["contentName"] == "faq" for item in rows)
        faq = next(item for item in rows if item["contentName"] == "faq")
        assert faq.get("statusKey") == "ready"
        assert faq.get("status") == 2
        detail = client.get("/api/AIKmss/GetDocument", params={"Id": faq["id"]}, headers=headers)
        assert detail.status_code == 200
        assert "10086" in detail.json()["content"]
        assert isinstance(detail.json().get("chunks"), list)

        updated = client.post(
            "/api/AIKmss/UpdateDocument",
            headers=headers,
            json={"id": faq["id"], "contentName": "faq", "content": "\u9000\u8d27\u97007\u5929\u5185\u7533\u8bf7\u3002\u5ba2\u670d\u7535\u8bdd10086\u3002\u6362\u8d27\u540c\u6837\u9002\u7528\u3002"},
        )
        assert updated.status_code == 200, updated.text

        upload = client.post("/api/File/UploadFile", headers=headers, files={"file": ("policy.pdf", pdf_bytes, "application/pdf")})
        assert upload.status_code == 200, upload.text
        file_id = str(upload.json()["id"])
        fetched = client.get("/api/File/GetById", params={"Id": file_id}, headers=headers)
        assert fetched.status_code == 200, fetched.text
        private_import = client.post("/api/AIKmss/ImportFile", params={"Id": str(kb_id), "fileId": file_id}, headers=headers)
        assert private_import.status_code == 400, private_import.text
        assert "请先显式共享文件" in error_text(private_import)
        shared = client.post("/api/File/SetSharing", headers=headers, json={"id": file_id, "accessMode": "tenant"})
        assert shared.status_code == 200, shared.text
        assert shared.json()["accessMode"] == "tenant"
        imported_file = client.post("/api/AIKmss/ImportFile", params={"Id": str(kb_id), "fileId": file_id}, headers=headers)
        assert imported_file.status_code == 200, imported_file.text
        assert "detail" not in imported_file.json() or "\u6587\u4ef6 ID \u65e0\u6548" not in error_text(imported_file)
        assert imported_file.json()['status']=='queued'
        consume_import(client,imported_file,headers,monkeypatch)
        after_upload = client.get("/api/AIKmss/GetDocuments", params={"Id": kb_id}, headers=headers).json()
        assert len(after_upload) >= 2
        assert any(item.get("statusKey") == "ready" for item in after_upload)

        trace = client.get("/api/Knowledge/RetrievalTrace", params={"knowledgeBaseId": kb_id, "query": "\u9000\u8d27", "limit": 5}, headers=headers)
        assert trace.status_code == 200, trace.text
        assert "results" in trace.json()

        recall = client.post("/api/AIKmss/RecallTest", headers=headers, json={"knowledgeBaseId": kb_id, "query": "\u9000\u8d27", "k": 5, "retrievalStrategy": "advanced"})
        assert recall.status_code == 200, recall.text
        assert isinstance(recall.json().get("results"), list)
        assert recall.json().get("strategy") == "advanced"

        eval_set = client.post("/api/AIKmss/SaveEvalSet", headers=headers, json={"knowledgeBaseId": kb_id, "name": f"eval-{stamp}"}).json()
        case = client.post(
            "/api/AIKmss/SaveEvalCase",
            headers=headers,
            json={"evalSetId": eval_set["id"], "query": "\u9000\u8d27", "expectedDocumentIds": [faq["id"]]},
        )
        assert case.status_code == 200, case.text
        ran = client.post("/api/AIKmss/RunEvalSet", headers=headers, json={"evalSetId": eval_set["id"], "k": 5})
        assert ran.status_code == 200, ran.text
        summary = ran.json()
        assert "hitAtK" in summary and "recallAtK" in summary and "mrr" in summary
        assert "misses" in summary

        removed = client.delete("/api/AIKmss/DeleteDocument", params={"Id": faq["id"]}, headers=headers)
        assert removed.status_code == 200
        remaining = client.get("/api/AIKmss/GetDocuments", params={"Id": kb_id}, headers=headers).json()
        assert all(item["id"] != faq["id"] for item in remaining)

        assert client.post(
            "/api/AISkillToolManagement/AddEdit",
            headers=headers,
            json={"name": "\u5f85\u529e\u62c6\u89e3", "classMethod": "split_todo", "skillToolType": 1, "activeStatus": 1},
        ).json() is True
        skills = client.post("/api/AISkillToolManagement/GetPageData", headers=headers, json={}).json()["data"]
        assert any(item["name"] == "\u5f85\u529e\u62c6\u89e3" for item in skills)


def test_agent_multiselect_and_knowledge_all_list(monkeypatch) -> None:
    stamp = str(int(time.time() * 1000))
    with TestClient(app) as client:
        headers = login(client,monkeypatch)
        prompt_names = [f"prompt-a-{stamp}", f"prompt-b-{stamp}"]
        for name in prompt_names:
            created = client.post(
                "/api/AIPrompts/AddEdit",
                headers=headers,
                json={"name": name, "content": f"{name} content", "describe": name},
            )
            assert created.status_code == 200, created.text
            assert created.json() is True
        prompts = client.get("/api/AIPrompts/GetALLList", headers=headers)
        assert prompts.status_code == 200, prompts.text
        prompt_ids = [item["id"] for item in prompts.json() if item.get("name") in prompt_names]
        assert len(prompt_ids) == 2

        kb_name = f"kb-bind-{stamp}"
        assert client.post("/api/AIKmss/AddEdit", headers=headers, json={"name": kb_name, "describe": "bind"}).json() is True
        all_kbs = client.get("/api/AIKmss/GetALLList", headers=headers)
        assert all_kbs.status_code == 200, all_kbs.text
        assert isinstance(all_kbs.json(), list)
        kb = next(item for item in all_kbs.json() if item.get("name") == kb_name)
        kb_id = kb["id"]

        imported = client.post(
            "/api/AIKmss/ImportText",
            headers=headers,
            json={"knowledgeBaseId": kb_id, "contentName": "policy", "content": "\u9000\u8d27\u97007\u5929\u5185\u7533\u8bf7\u3002"},
        )
        assert imported.status_code == 200, imported.text
        assert imported.json()['status']=='queued_text'
        consume_import(client,imported,headers,monkeypatch)
        docs = client.get("/api/AIKmss/GetDocuments", params={"Id": kb_id}, headers=headers).json()
        policy = next(item for item in docs if item["contentName"] == "policy")
        assert policy["statusKey"] == "ready"

        app_name = f"agent-bind-{stamp}"
        saved = client.post(
            "/api/AIApps/AddEdit",
            headers=headers,
            json={
                "name": app_name,
                "describe": "multi bind",
                "type": "chat",
                "aiPromptIDs": prompt_ids,
                "kmsIds": [kb_id],
            },
        )
        assert saved.status_code == 200, saved.text
        assert saved.json() is True
        apps = client.get("/api/AIApps/GetALLList", headers=headers).json()
        agent = next(item for item in apps if item.get("name") == app_name)
        details = client.get("/api/AIApps/GetDetails", params={"id": agent["id"]}, headers=headers)
        assert details.status_code == 200, details.text
        payload = details.json()
        assert {str(item) for item in payload.get("aiPromptIDs") or []} == {str(item) for item in prompt_ids}
        assert {str(item) for item in payload.get("kmsIds") or []} == {str(kb_id)}
