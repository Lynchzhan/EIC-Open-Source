"""使用安全白名单契约生成实测 span 与独立瞬时事件。"""
import hashlib
import json
from datetime import datetime, timezone

from eic.core.langfuse_contract import build_ingestion_events


def _id(value, size):
    return hashlib.sha256(('eic-otlp-v1:' + value).encode('ascii')).hexdigest()[:size]


def otlp_trace_id(legacy_trace_id):
    """导出与页面外链共用同一 Trace ID 域。"""
    return _id(legacy_trace_id, 32)


def _nanos(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    delta = stamp - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return str((delta.days * 86400 + delta.seconds) * 1000000000 + delta.microseconds * 1000)


def _attribute(key, value):
    if isinstance(value, bool):
        wrapped = {'boolValue': value}
    elif isinstance(value, int):
        wrapped = {'intValue': str(value)}
    elif isinstance(value, float):
        wrapped = {'doubleValue': value}
    else:
        wrapped = {'stringValue': str(value)}
    return {'key': key, 'value': wrapped}


def build_completed_spans(row, *, environment, namespace):
    events = build_ingestion_events(row, environment=environment, namespace=namespace)
    if not events:
        return []
    trace, event = events[0]['body'], events[1]
    body = event['body']
    complete = 'endTime' in body and 'startTime' in body
    # 事务收据与状态快照只有发生时间，作为瞬时事件展示，不推算执行耗时或成功终态。
    instant = body.get('startTime') or body.get('endTime') or event['timestamp']
    start, end = (_nanos(body['startTime']), _nanos(body['endTime'])) if complete else (_nanos(instant), _nanos(instant))
    if int(end) < int(start):
        return []
    # 新 transport 独立 trace 域，避免与 legacy 同项目双写同一 ID。
    trace_id = otlp_trace_id(body['traceId'])
    span_id = _id(body['id'] if complete else body['id'] + ':event:' + event['id'], 16)
    kind = ('generation' if event['type'] == 'generation-create' else 'span') if complete else 'event'
    attrs = [
        _attribute('langfuse.observation.type', kind),
        _attribute('langfuse.environment', environment),
        _attribute('langfuse.trace.name', trace['name']),
        _attribute('langfuse.observation.level', body.get('level', 'DEFAULT')),
        _attribute('langfuse.observation.status_message', body.get('statusMessage', '')),
        _attribute('langfuse.observation.metadata.timing_kind', 'measured_span' if complete else 'instant_event'),
    ]
    for key, value in body['metadata'].items():
        attrs.append(_attribute('langfuse.observation.metadata.' + key, value))
    if body.get('model'):
        attrs.append(_attribute('gen_ai.request.model', body['model']))
    for original, target in [('userId', 'langfuse.user.id'), ('sessionId', 'langfuse.session.id')]:
        if original in trace:
            attrs.append(_attribute(target, trace[original]))
    if body.get('usageDetails'):
        attrs.append(_attribute('langfuse.observation.usage_details', json.dumps(body['usageDetails'], separators=(',', ':'))))
    span = {'traceId': trace_id, 'spanId': span_id, 'name': body['name'], 'kind': 1,
            'startTimeUnixNano': start, 'endTimeUnixNano': end, 'attributes': attrs,
            'status': {'code': 2 if body.get('level') == 'ERROR' else
                       1 if body.get('statusMessage') in {'success', 'completed', 'closed', 'ready'} else 0}}
    # 仅引用契约中真实存在的父关系，不能为瞬时收据虚构一个完整业务 span。
    if body.get('parentObservationId'):
        span['parentSpanId'] = _id(body['parentObservationId'], 16)
    return [{'id': trace_id + ':' + span_id, 'type': 'otlp-completed-span',
             'timestamp': event['timestamp'], 'body': span}]
