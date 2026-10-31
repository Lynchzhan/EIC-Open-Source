"""模型删除影响预览及显式恢复；不改写历史执行快照。"""
from copy import deepcopy
import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import select, update

from eic.core.ids import new_id
from eic.models import AIModel, AIApp, KnowledgeBase, KnowledgeDocument, Resource, Tenant, TenantModelDefaults

CHAT_KEYS = {'chatModelID', 'chatModelId', 'aIModelId', 'AIModelId', 'summaryModelId', 'extractionModelId', 'modelId'}
EMBED_KEYS = {'aIModelsId', 'embeddingModelId', 'memoryEmbeddingModelId'}
RERANK_KEYS = {'aIRerankModelsId', 'rerankModelId'}
KINDS = {'chat': CHAT_KEYS, 'embedding': EMBED_KEYS, 'rerank': RERANK_KEYS}
LIST_KEYS = {'fallbackModelIds', 'lowCostModelIds'}


def paths(value, keys, target, prefix=()):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in LIST_KEYS and keys == CHAT_KEYS and isinstance(item, list):
                for index, entry in enumerate(item):
                    if str(entry) == str(target):
                        yield prefix + (key, index)
            elif key in keys and str(item) == str(target):
                yield prefix + (key,)
            else:
                yield from paths(item, keys, target, prefix + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from paths(item, keys, target, prefix + (index,))


def read_path(value, path):
    try:
        for part in path:
            value = value[part]
        return value
    except (KeyError, IndexError, TypeError):
        return '__missing__'


def write_path(value, path, replacement):
    for part in path[:-1]:
        value = value[part]
    value[path[-1]] = replacement


async def lock_tenant(session, tenant_id):
    # SQLite 同样取得写锁，模型保存与删除共用这一把锁。
    current = session.sync_session.get_transaction()
    if current is not None and session.info.get('model_tenant_lock') == (tenant_id, current):
        return
    if not session.new and not session.dirty and not session.deleted:
        await session.rollback()
    result = await session.execute(update(Tenant).where(Tenant.id == tenant_id,
        Tenant.active.is_(True)).values(id=Tenant.id))
    if result.rowcount != 1:
        raise HTTPException(401, detail={'errMsg': '租户不可用'})
    session.info['model_tenant_lock'] = (tenant_id, session.sync_session.get_transaction())


async def references(session, tenant_id, model):
    result = []
    def add(row, attr, path=()):
        result.append((row, attr, path))
    for row in await session.scalars(select(AIApp).where(AIApp.tenant_id == tenant_id)):
        if row.chat_model_id == model.id:
            add(row, 'chat_model_id')
        for path in paths(row.config, KINDS[model.model_type], model.id):
            add(row, 'config', path)
    for row in await session.scalars(select(KnowledgeBase).where(KnowledgeBase.tenant_id == tenant_id)):
        for attr in ('embedding_model_id', 'rerank_model_id'):
            if getattr(row, attr) == model.id:
                add(row, attr)
    defaults = await session.get(TenantModelDefaults, tenant_id)
    if defaults:
        for attr in ('embedding_model_id', 'rerank_model_id'):
            if getattr(defaults, attr) == model.id:
                add(defaults, attr)
    for row in await session.scalars(select(Resource).where(Resource.tenant_id == tenant_id,
            Resource.resource_type.in_(['ai_apps', 'knowledge_bases', 'ai_kmss', 'tenant_chat_default']), Resource.is_deleted.is_(False))):
        for path in paths(row.data, KINDS[model.model_type], model.id):
            add(row, 'data', path)
    return result


def public_ref(ref):
    row, attr, path = ref
    identity = getattr(row, 'id', None) or row.tenant_id
    key = json.dumps([row.__tablename__, str(identity), attr, path], separators=(',', ':'))
    field = str(path[-1]) if path else attr
    label = {'chat_model_id': '主对话模型', 'chatModelID': '主对话模型', 'chatModelId': '主对话模型',
             'aIModelId': '主对话模型', 'AIModelId': '主对话模型', 'summaryModelId': '上下文压缩模型',
             'extractionModelId': '记忆提取模型', 'embedding_model_id': '向量模型', 'embeddingModelId': '向量模型',
             'memoryEmbeddingModelId': '记忆向量模型', 'aIModelsId': '向量模型',
             'rerank_model_id': '重排模型', 'rerankModelId': '重排模型', 'aIRerankModelsId': '重排模型',
             'modelId': '默认模型'}.get(field, '备用模型')
    if 'tiers' in path:
        index = path.index('tiers')
        label = f'降级策略第 {int(path[index + 1]) + 1} 档'
    return {'key': key, 'name': getattr(row, 'name', None) or '租户默认模型', 'location': label}


def fingerprint(refs):
    return hashlib.sha256(json.dumps(sorted(public_ref(r)['key'] for r in refs)).encode()).hexdigest()


def value_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


async def impact(session, tenant_id, model):
    refs = await references(session, tenant_id, model)
    alternatives = list(await session.scalars(select(AIModel).where(AIModel.tenant_id == tenant_id,
        AIModel.model_type == model.model_type, AIModel.enabled.is_(True), AIModel.id != model.id).order_by(AIModel.updated_at.desc())))
    display = {}
    for ref in refs:
        row = ref[0]; item = public_ref(ref)
        display[(getattr(row, 'id', row.tenant_id), item['location'])] = item
    return {'references': list(display.values()), 'revision': fingerprint(refs),
            'alternatives': [{'id': str(m.id), 'name': m.model_name} for m in alternatives],
            'requiresReindex': model.model_type == 'embedding'}


async def invalidate_embedding(session, row):
    row.status = 'reindex_required'
    await session.execute(update(KnowledgeDocument).where(KnowledgeDocument.tenant_id == row.tenant_id,
        KnowledgeDocument.knowledge_base_id == row.id, KnowledgeDocument.is_deleted.is_(False)).values(
            active_index_version=0, pending_index_version=None, embedding_status='pending',
            index_status='pending', status='pending', error_message='向量模型已变更，请重新入库'))


async def remove(session, tenant_id, model, replacement_id, expected_revision):
    refs = await references(session, tenant_id, model)
    if fingerprint(refs) != expected_revision:
        raise HTTPException(409, detail={'errMsg': '模型引用已变化，请重新查看删除影响'})
    replacement = None
    if replacement_id:
        replacement = await session.get(AIModel, int(replacement_id))
        if not replacement or replacement.tenant_id != tenant_id or not replacement.enabled or replacement.id == model.id or replacement.model_type != model.model_type:
            raise HTTPException(400, detail={'errMsg': '替代模型不存在或类型不匹配'})
    value = replacement.id if replacement else None
    recovery = []
    touched = {}
    previous_hashes = {(row.__tablename__, str(getattr(row, 'id', row.tenant_id)), attr): value_hash(getattr(row, attr))
                       for row, attr, _ in refs}
    previous_recoveries = await pending(session, tenant_id, model.model_type)
    for row, attr, path in refs:
        touched[(row.__tablename__, getattr(row, 'id', row.tenant_id), attr)] = (row, attr)
        previous = deepcopy(getattr(row, attr))
        if path:
            changed = deepcopy(previous)
            write_path(changed, path, str(value) if value else None)
            # 清空档位后显式停止降级，保留位置用于后续恢复。
            policy = changed.get('runtimePolicy') if isinstance(changed, dict) else None
            if policy and any(not t.get('modelId') for t in policy.get('tiers', [])):
                policy['action'] = 'stop'
            setattr(row, attr, changed)
        else:
            setattr(row, attr, value)
        if isinstance(row, KnowledgeBase) and attr == 'embedding_model_id':
            await invalidate_embedding(session, row)
        if value is None:
            public = public_ref((row, attr, path))
            record = Resource(id=await new_id(session), tenant_id=tenant_id,
                resource_type='model_reference_recovery', name=public['name'],
                data={'kind': model.model_type, 'table': row.__tablename__,
                      'rowId': str(getattr(row, 'id', None) or tenant_id), 'attribute': attr,
                      'path': list(path), 'location': public['location']})
            session.add(record)
            recovery.append((record, row, attr))
    if value is not None:
        # 替换成已在降级表中的模型时合并重复档位；硬预算本身保持原值。
        for row, attr in touched.values():
            current = getattr(row, attr)
            policy = current.get('runtimePolicy') if isinstance(current, dict) else None
            if not policy:
                continue
            changed = deepcopy(current)
            seen, tiers = set(), []
            for tier in changed['runtimePolicy'].get('tiers', []):
                key = str(tier.get('modelId'))
                if key not in seen:
                    tiers.append(tier); seen.add(key)
            changed['runtimePolicy']['tiers'] = tiers
            setattr(row, attr, changed)
    for record, row, attr in recovery:
        record.data = {**record.data, 'expectedHash': value_hash(getattr(row, attr))}
    # 后续模型删除属于已知系统变更，保持先前仍有效的待恢复记录可用；不接纳用户改过的旧快照。
    for record in previous_recoveries:
        data = record.data
        key = (data['table'], data['rowId'], data['attribute'])
        if key in previous_hashes and data.get('expectedHash') == previous_hashes[key]:
            row, attr = touched[(key[0], int(key[1]), key[2])]
            record.data = {**data, 'expectedHash': value_hash(getattr(row, attr))}
    await session.flush()
    await session.delete(model)


async def pending(session, tenant_id, kind):
    return [row for row in await session.scalars(select(Resource).where(Resource.tenant_id == tenant_id,
        Resource.resource_type == 'model_reference_recovery', Resource.is_deleted.is_(False))) if row.data.get('kind') == kind]


async def pending_choices(session, tenant_id, kind):
    groups = {}
    for record in await pending(session, tenant_id, kind):
        key = record.data['rowId']
        if key not in groups:
            groups[key] = {'id': str(record.id), 'name': record.name, 'locations': set()}
        groups[key]['locations'].add(record.data['location'])
    return [{'id': r['id'], 'name': r['name'], 'location': '、'.join(sorted(r['locations']))} for r in groups.values()]


async def restore(session, tenant_id, model, ids, set_default=False):
    tables = {c.__tablename__: c for c in (AIApp, KnowledgeBase, Resource, TenantModelDefaults)}
    selected = set(map(str, ids))
    available = await pending(session, tenant_id, model.model_type)
    if selected - {str(r.id) for r in available}:
        raise HTTPException(409, detail={'errMsg': '待恢复引用已变化，请刷新'})
    # 兼容 Resource 与规范表是同一个业务对象；勾选后按业务对象整体恢复，避免两份配置分叉。
    groups = {r.data['rowId'] for r in available if str(r.id) in selected}
    selected.update(str(r.id) for r in available if r.data['rowId'] in groups)
    resolved = []
    for record in available:
        if str(record.id) not in selected:
            continue
        data = record.data
        cls = tables.get(data['table'])
        row = await session.get(cls, int(data['rowId'])) if cls else None
        if not row or row.tenant_id != tenant_id or getattr(row, 'is_deleted', False):
            raise HTTPException(409, detail={'errMsg': '引用目标已不存在，请重新选择'})
        attr, path = data['attribute'], data['path']
        current = getattr(row, attr)
        if value_hash(current) != data.get('expectedHash') or (read_path(current, path) if path else current) is not None:
            raise HTTPException(409, detail={'errMsg': '引用已被修改，不能覆盖，请刷新'})
        resolved.append((record, row, attr, path))
    for record, row, attr, path in resolved:
        current = getattr(row, attr)
        if path:
            changed = deepcopy(current)
            write_path(changed, path, str(model.id))
            setattr(row, attr, changed)
        else:
            setattr(row, attr, model.id)
        if isinstance(row, KnowledgeBase) and attr == 'embedding_model_id':
            await invalidate_embedding(session, row)
        record.is_deleted = True
    if set_default:
        defaults = await session.get(TenantModelDefaults, tenant_id)
        if model.model_type != 'chat':
            if not defaults:
                defaults = TenantModelDefaults(tenant_id=tenant_id)
                session.add(defaults)
            setattr(defaults, model.model_type + '_model_id', model.id)
        else:
            default = await session.scalar(select(Resource).where(Resource.tenant_id == tenant_id,
                Resource.resource_type == 'tenant_chat_default', Resource.is_deleted.is_(False)))
            if not default:
                default = Resource(id=await new_id(session), tenant_id=tenant_id,
                    resource_type='tenant_chat_default', name='租户默认聊天模型')
                session.add(default)
            default.data = {'modelId': str(model.id)}
