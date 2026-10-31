"""models 领域路由；不导入应用入口或复制全局命名空间。"""
from types import SimpleNamespace
import asyncio
import time
from typing import Any, Literal
from fastapi import Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from eic.ai.budget import ChatConfigurationError
from eic.ai.providers import ModelProviderError, chat_parameters
from eic.ai import search as chat_search
from eic.core.authentication import Principal
from eic.core.database import get_session
from eic.core.ids import new_id
from eic.core.security import decrypt_secret, encrypt_secret_if_changed, secret_hint
from eic import model_lifecycle
from eic.models import AIModel
from eic.api.schemas import AIModelInput, AIModelTestInput, PageInput


def bind_routes(app, *, current_principal, ensure_permission, first_int, legacy_model_type, legacy_provider, normalize_endpoint, normalize_model_type, normalize_provider, page_result, probe_model_connection):
    def serialize_model(model: AIModel) -> dict[str, Any]:
        return {
            "id": model.id, "provider": model.provider, "modelType": model.model_type, "endpoint": model.endpoint,
            "modelName": model.model_name, "description": model.description,
            "aiType": model.provider if model.provider in {'openai_responses', 'anthropic'} else legacy_provider(model.provider), "aiModelType": legacy_model_type(model.model_type),
            "endPoint": model.endpoint, "modelDescription": model.description,
            "embeddingValueSize": model.embedding_dimensions, "maxAskPromptSize": model.max_input_tokens,
            "answerTokens": model.max_output_tokens, "secretConfigured": bool(model.secret_encrypted),
            "chatParameterProfile": model.chat_parameter_profile,
            "reasoningPolicy": model.reasoning_policy,
            "searchMode": model.search_mode,
            "secretHint": model.secret_hint, "enabled": model.enabled,
        }


    @app.post("/api/AIModels/GetPageData")
    async def ai_models_page(payload: PageInput, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        await ensure_permission("ai.model.read", principal, session)
        criteria = [AIModel.tenant_id == principal.tenant_id]
        if payload.searchKey:
            criteria.append(AIModel.model_name.contains(payload.searchKey))
        total = await session.scalar(select(func.count()).select_from(AIModel).where(*criteria)) or 0
        models = await session.scalars(select(AIModel).where(*criteria).order_by(AIModel.updated_at.desc()).offset((payload.pageNum - 1) * payload.pageSize).limit(payload.pageSize))
        return page_result([serialize_model(model) for model in models], payload, total)


    @app.get("/api/AIModels/GetALLList")
    async def ai_models_list(Type: str | None = None, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
        await ensure_permission("ai.model.read", principal, session)
        statement = select(AIModel).where(AIModel.tenant_id == principal.tenant_id, AIModel.enabled.is_(True))
        if Type:
            wanted = normalize_model_type(Type)
            statement = statement.where(AIModel.model_type.in_({wanted, str(Type), str(legacy_model_type(wanted))}))
        return [serialize_model(model) for model in await session.scalars(statement)]


    @app.get("/api/AIModels/Defaults")
    async def model_defaults_get(principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)):
        await ensure_permission("knowledge.read", principal, session)
        from eic.models import TenantModelDefaults
        row = await session.get(TenantModelDefaults, principal.tenant_id)
        return {"embeddingModelId": str(row.embedding_model_id) if row and row.embedding_model_id else None,
                "rerankModelId": str(row.rerank_model_id) if row and row.rerank_model_id else None}


    @app.post("/api/AIModels/Defaults")
    async def model_defaults_save(payload: dict[str, Any], principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)):
        await ensure_permission("ai.model.write", principal, session)
        await model_lifecycle.lock_tenant(session, principal.tenant_id)
        from eic.models import TenantModelDefaults
        from eic.knowledge.model_defaults import validated_model
        embedding = await validated_model(session, principal.tenant_id, payload.get("embeddingModelId"), "embedding")
        rerank = await validated_model(session, principal.tenant_id, payload.get("rerankModelId"), "rerank")
        row = await session.get(TenantModelDefaults, principal.tenant_id, with_for_update=True)
        if row is None:
            row = TenantModelDefaults(tenant_id=principal.tenant_id)
            session.add(row)
        row.embedding_model_id, row.rerank_model_id = embedding, rerank
        await session.commit()
        return True


    @app.post("/api/AIModels/AddEdit")
    async def ai_models_add_edit(payload: AIModelInput, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        await ensure_permission("ai.model.write", principal, session)
        await model_lifecycle.lock_tenant(session, principal.tenant_id)
        model_id = first_int(payload.id)
        model = await session.get(AIModel, model_id) if model_id else None
        if model_id and model is None:
            raise HTTPException(404, detail={'errMsg': '模型不存在或已删除，请刷新'})
        if model is not None and model.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail={"code": 400, "errMsg": "模型不存在"})
        provider = normalize_provider(payload.provider)
        model_type = normalize_model_type(payload.model_type)
        if model is not None and model.model_type != model_type:
            if await model_lifecycle.references(session, principal.tenant_id, model):
                raise HTTPException(409, detail={'errMsg': '模型仍被引用，不能改变模型类型；请新建模型后迁移引用'})
        if provider in {'openai_responses', 'anthropic'} and model_type != 'chat':
            raise HTTPException(400, detail={'errMsg': 'Responses 和 Messages 仅用于聊天模型'})
        endpoint = normalize_endpoint(payload.endpoint)
        if model_type == 'chat' and provider in {'openai_compatible', 'openai_responses', 'anthropic'}:
            from eic.ai.protocol_bridge import base_url
            try:
                endpoint = base_url(endpoint)
            except ModelProviderError:
                raise HTTPException(400, detail={'errMsg': '模型地址无效'}) from None
        if not endpoint or not payload.model_name.strip():
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "模型地址和模型名称不能为空"})
        dimensions = payload.embedding_dimensions if isinstance(payload.embedding_dimensions, int) and payload.embedding_dimensions > 0 else None
        if model_type == "embedding" and dimensions is None:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "Embedding 模型必须填写矢量维度"})
        if model is None:
            model = AIModel(id=await new_id(session), tenant_id=principal.tenant_id, provider=provider, model_type=model_type, endpoint=endpoint, model_name=payload.model_name.strip())
            session.add(model)
        model.provider, model.model_type, model.endpoint, model.model_name = provider, model_type, endpoint, payload.model_name.strip()
        model.description, model.embedding_dimensions = payload.description, dimensions
        model.max_input_tokens, model.max_output_tokens = payload.max_input_tokens, payload.max_output_tokens
        model.chat_parameter_profile = payload.chat_parameter_profile
        if payload.search_mode != 'disabled' and model_type != 'chat':
            raise HTTPException(400, detail={'errMsg': '只有聊天模型可以配置联网搜索'})
        if payload.search_mode == 'native' and provider != 'openai_responses':
            raise HTTPException(400, detail={'errMsg': chat_search.ERRORS['search_native_unsupported']})
        model.search_mode = payload.search_mode
        from eic.ai.reasoning_policy import normalize_policy as normalize_reasoning_policy
        try:
            model.reasoning_policy = normalize_reasoning_policy(payload.reasoning_policy, provider, payload.max_output_tokens)
        except ValueError as exc:
            raise HTTPException(400, detail={"errMsg": str(exc)}) from None
        if model_type != "chat" and model.reasoning_policy["enabled"]:
            raise HTTPException(400, detail={"errMsg": "只有聊天模型可以配置思考模式"})
        if model_type == "chat":
            try:
                chat_parameters(model)
            except ChatConfigurationError as exc:
                raise HTTPException(status_code=400, detail={"code": 400, "errMsg": f"模型参数能力不匹配：{exc.reason}"}) from exc
        if payload.api_key:
            model.secret_encrypted = encrypt_secret_if_changed(payload.api_key, model.secret_encrypted)
            model.secret_hint = secret_hint(payload.api_key)
        model.description = (payload.description or "")[:500]
        try:
            await session.flush()
            await model_lifecycle.restore(session, principal.tenant_id, model, payload.restore_reference_ids, payload.set_default)
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "模型保存失败：名称或配置与现有记录冲突，可改部署名后重试"})
        except SQLAlchemyError:
            await session.rollback()
            raise HTTPException(status_code=500, detail={"code": 500, "errMsg": "模型保存失败"})
        return True


    @app.delete("/api/AIModels/Delete")
    async def ai_models_delete(Id: int, revision: str = '', replacementId: int | None = None, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        await ensure_permission("ai.model.write", principal, session)
        await model_lifecycle.lock_tenant(session, principal.tenant_id)
        model = await session.get(AIModel, Id)
        if model is None or model.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "模型不存在或已删除"})
        await model_lifecycle.remove(session, principal.tenant_id, model, replacementId, revision)
        await session.commit()
        return True


    @app.get('/api/AIModels/DeleteImpact')
    async def model_delete_impact(Id: int, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)):
        await ensure_permission('ai.model.write', principal, session)
        model = await session.get(AIModel, Id)
        if not model or model.tenant_id != principal.tenant_id:
            raise HTTPException(404, detail={'errMsg': '模型不存在'})
        return await model_lifecycle.impact(session, principal.tenant_id, model)


    @app.get('/api/AIModels/PendingReferences')
    async def model_pending_references(kind: Literal['chat', 'embedding', 'rerank'], principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)):
        await ensure_permission('ai.model.write', principal, session)
        return await model_lifecycle.pending_choices(session, principal.tenant_id, kind)


    @app.get('/api/AIModels/Secret')
    async def model_secret(Id: int, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)):
        await ensure_permission('ai.model.write', principal, session)
        model = await session.get(AIModel, Id)
        if not model or model.tenant_id != principal.tenant_id:
            raise HTTPException(404, detail={'errMsg': '模型不存在'})
        return JSONResponse({'code': 200, 'data': {'secret': decrypt_secret(model.secret_encrypted) if model.secret_encrypted else ''}},
                            headers={'Cache-Control': 'no-store', 'Pragma': 'no-cache'})


    @app.post('/api/AIModels/FetchModels')
    async def model_fetch_models(payload: AIModelInput, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)):
        await ensure_permission('ai.model.write', principal, session)
        secret = payload.api_key or ''
        if payload.id and not secret:
            model = await session.get(AIModel, int(payload.id))
            if not model or model.tenant_id != principal.tenant_id:
                raise HTTPException(404, detail={'errMsg': '模型不存在'})
            secret = decrypt_secret(model.secret_encrypted) if model.secret_encrypted else ''
        from eic.ai.protocol_bridge import fetch_models
        return await fetch_models(normalize_endpoint(payload.endpoint), normalize_provider(payload.provider), secret, principal.tenant_id)


    @app.post("/api/AIModels/TestConnection")
    async def ai_models_test_connection(payload: AIModelTestInput, request: Request, response: Response,
                                       principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        await ensure_permission("ai.model.write", principal, session)
        response.headers["Cache-Control"] = "no-store"
        stored = None
        if payload.id:
            stored = await session.get(AIModel, payload.id)
            if stored is None or stored.tenant_id != principal.tenant_id:
                raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "模型不存在"})
        if payload.config_source == 'saved' and stored is None:
            raise HTTPException(400, detail={'errMsg': '请选择已保存模型'})
        secret = '' if payload.config_source == 'saved' else (payload.api_key or "").strip()
        if not secret and stored is not None:
            secret = decrypt_secret(stored.secret_encrypted) if stored.secret_encrypted else ""
        if not secret:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "请先填写模型秘钥"})
        use_stored = stored is not None and (payload.config_source == 'saved' or
            payload.config_source is None and not ((payload.endpoint or "").strip() and (payload.model_name or "").strip()))
        if use_stored:
            endpoint = stored.endpoint
            model_name = stored.model_name
            provider = stored.provider
            model_type = stored.model_type
        else:
            endpoint = normalize_endpoint(payload.endpoint)
            model_name = (payload.model_name or "").strip()
            provider = normalize_provider(payload.provider)
            model_type = normalize_model_type(payload.model_type)
        if not endpoint or not model_name:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "模型地址和模型名称不能为空"})
        if model_type == 'chat' and provider in {'openai_compatible', 'openai_responses', 'anthropic'}:
            from eic.ai.protocol_bridge import base_url
            try:
                endpoint = base_url(endpoint)
            except ModelProviderError:
                raise HTTPException(400, detail={'errMsg': '模型地址无效'}) from None
        dimensions = stored.embedding_dimensions if use_stored else payload.embedding_dimensions
        dimensions = dimensions if isinstance(dimensions, int) and dimensions > 0 else None
        if model_type == "embedding" and dimensions is None:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "Embedding 模型必须填写矢量维度"})
        probe = AIModel(
            id=payload.id or 0,
            tenant_id=principal.tenant_id,
            provider=provider,
            model_type=model_type,
            endpoint=endpoint,
            model_name=model_name,
            embedding_dimensions=dimensions,
            chat_parameter_profile=stored.chat_parameter_profile if use_stored else payload.chat_parameter_profile,
            max_input_tokens=stored.max_input_tokens if use_stored else payload.max_input_tokens,
            max_output_tokens=stored.max_output_tokens if use_stored else payload.max_output_tokens,
            reasoning_policy=stored.reasoning_policy if use_stored else payload.reasoning_policy,
            search_mode='disabled',
        )
        from eic import model_test_preferences
        from eic.ai.providers import chat_parameters
        from eic.ai.budget import ChatConfigurationError
        settings = await model_test_preferences.read_current(principal, session) if model_type == 'chat' else None
        if settings is not None and payload.expected_preferences_revision is not None and settings['revision'] != payload.expected_preferences_revision:
            raise HTTPException(409, detail={'errMsg': '测试设置已被其他设备修改，本次未发送模型请求；请重新测试。',
                'reason': 'test_preferences_changed'}, headers={'Cache-Control': 'no-store'})
        prompt = settings['prompt'] if settings else 'ping'
        timeout_seconds = settings['timeoutSeconds'] if settings else 15
        diagnostics = {'modelName': model_name, 'provider': provider, 'modelType': model_type,
            'configSource': 'saved' if use_stored else 'form', 'timeoutSeconds': timeout_seconds,
            'prompt': prompt if settings else None, 'preferencesRevision': settings['revision'] if settings else None}
        started = time.perf_counter()
        operation = None
        try:
            if model_type == 'chat':
                _, parameter_info = chat_parameters(probe)
                diagnostics['parameters'] = parameter_info
            # 探测前结束只读事务，避免长时间持有账号或模型读取事务。
            await session.rollback()
            operation = asyncio.create_task(probe_model_connection(probe, secret,
                prompt=prompt, timeout_seconds=timeout_seconds))
            while True:
                done, _ = await asyncio.wait({operation}, timeout=0.25)
                if done:
                    result = operation.result()
                    break
                if await request.is_disconnected():
                    raise HTTPException(499, detail={'errMsg': '客户端已取消测试', 'reason': 'test_cancelled'})
                if time.perf_counter() - started >= timeout_seconds:
                    raise ModelProviderError('模型测试超时；未自动重发，请核对上游状态后手动重试。',
                                             reason='provider_timeout', retryable=False)
        except ChatConfigurationError as exc:
            raise HTTPException(400, detail={'errMsg': '测试参数配置无效，请检查输入、输出预算及思考档位。',
                'reason': exc.reason, 'diagnostics': diagnostics,
                'elapsedMs': round((time.perf_counter() - started) * 1000)}, headers={'Cache-Control': 'no-store'}) from None
        except ModelProviderError as exc:
            raise HTTPException(status_code=400, detail={'code': 400, 'errMsg': str(exc),
                'reason': exc.reason, 'diagnostics': diagnostics,
                'elapsedMs': round((time.perf_counter() - started) * 1000)}, headers={'Cache-Control': 'no-store'}) from None
        finally:
            if operation is not None and not operation.done():
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
        if model_type == 'embedding':
            from eic.vector.contracts import ProjectionContractError, check_projection_dimensions
            try:
                diagnostics['projection'] = await check_projection_dimensions(dimensions)
            except ProjectionContractError as exc:
                diagnostics['projection'] = {'status': 'incompatible', 'code': str(exc)}
            except Exception:
                diagnostics['projection'] = {'status': 'unavailable', 'code': 'vector_store_unavailable'}
        return {'code': 200, 'isSuccess': True, 'data': {'ok': True, **result,
            'message': result.get('message', '模型响应正常'), 'diagnostics': diagnostics,
            'elapsedMs': round((time.perf_counter() - started) * 1000)}}
    return SimpleNamespace(serialize_model=serialize_model, ai_models_page=ai_models_page, ai_models_list=ai_models_list, model_defaults_get=model_defaults_get, model_defaults_save=model_defaults_save, ai_models_add_edit=ai_models_add_edit, ai_models_delete=ai_models_delete, model_delete_impact=model_delete_impact, model_pending_references=model_pending_references, model_secret=model_secret, model_fetch_models=model_fetch_models, ai_models_test_connection=ai_models_test_connection)
