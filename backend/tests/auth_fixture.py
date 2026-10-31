"""合成身份按产品认证合同签发；不得在生产认证层提供测试旁路。"""
from eic.core.security import create_access_token, credential_stamp


def signed_token(user_id, tenant_id, username, password_hash):
    return create_access_token(user_id, tenant_id, username,
                               auth_stamp=credential_stamp(password_hash))


def signed_headers(user_id, tenant_id, username, password_hash):
    return {"Authorization": "Bearer " + signed_token(user_id, tenant_id, username, password_hash)}
