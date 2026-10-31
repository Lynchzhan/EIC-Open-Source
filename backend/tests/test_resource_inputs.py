"""资源 DTO 的缺省、兼容输入与敏感字段负控。"""
from fastapi.exceptions import RequestValidationError
import pytest

from eic.api.resource_inputs import AIAppWrite, ChatMessageWrite, PromptWrite, TenantWrite, UserWrite, resource_payload, write_payload


def test_partial_updates_preserve_absence_and_extensions():
    payload = {'id': '123', 'customField': {'version': 2}, 'chatModelID': '19'}
    assert resource_payload('ai_apps', payload) == payload
    assert resource_payload('ai_apps', AIAppWrite.model_validate(payload)) == payload
    assert resource_payload('ai_apps', {'description': None}) == {'description': None}


def test_resource_payload_keeps_aliases_and_legacy_zero():
    data = {'id': 0, 'aIAppsId': '25', 'fileIds': '2,3', 'content': 'hello'}
    assert resource_payload('chat_messages', data) == data
    assert resource_payload('chats', {'id': '', 'name': '新会话'}) == {'id': '', 'name': '新会话'}


@pytest.mark.parametrize('payload', [{'id': True}, {'runtimePolicy': []}, {'fallbackModelIds': {}}, {'name': ['invalid']}])
def test_resource_payload_rejects_invalid_shapes(payload):
    with pytest.raises(RequestValidationError):
        write_payload(payload, AIAppWrite)


def test_secret_values_not_in_validation_errors():
    secret = 'not-a-real-secret-for-negative-control'
    with pytest.raises(RequestValidationError) as result:
        write_payload({'passWord': [secret]}, UserWrite)
    assert secret not in str(result.value.errors())
    assert all('input' not in error and 'ctx' not in error for error in result.value.errors())


def test_unset_fields_do_not_create_defaults():
    assert write_payload({}, ChatMessageWrite) == {}
    assert write_payload({'prompt': ''}, PromptWrite) == {'prompt': ''}


@pytest.mark.parametrize('code', [1000, '1000', None])
def test_tenant_code_preserves_numeric_or_string_payload(code):
    assert write_payload({'code': code}, TenantWrite) == {'code': code}


def test_tenant_boolean_code_is_not_a_numeric_identifier():
    with pytest.raises(RequestValidationError):
        write_payload({'code': True}, TenantWrite)
