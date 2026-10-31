"""organization 领域；通过显式装配提供跨域依赖。"""
from eic.api.resource_inputs import DepartmentWrite, PermissionWrite, PositionWrite, RoleWrite, TenantWrite, UserWrite, write_payload
from dataclasses import dataclass
from collections.abc import Callable
from types import SimpleNamespace as _RouteExports
from typing import Any
from eic.api.runtime import RuntimeBindings
from sqlalchemy.ext.asyncio import AsyncSession
from eic.models import Department
from fastapi import Depends
from fastapi import HTTPException
from eic.api.schemas import PageInput as PageInput
from eic.models import Permission
from eic.models import Position
from eic.core.authentication import Principal
from fastapi import Query
from fastapi import Response
from eic.models import Role
from eic.models import RolePermission
from eic.models import Tenant
from eic.models import User
from eic.models import UserRole
from eic.ai.stream_reader import complete_cleanup
from eic.core.organization import cycle_members
from sqlalchemy import delete
from sqlalchemy import func
from eic.core.database import get_session
from eic.core.security import hash_password
from eic.core import registration_codes
from sqlalchemy import select
import uuid
from eic.core.organization import validate_parent
from eic.core.security import verify_password


@dataclass(frozen=True)
class Dependencies:
    ensure_permission: Callable[..., Any]
    first_int: Callable[..., Any]
    has_permission: Callable[..., Any]
    legacy_status: Callable[..., Any]
    lock_tenant_writes: Callable[..., Any]
    new_entity_id: Callable[..., Any]
    new_id: Callable[..., Any]
    page_result: Callable[..., Any]
    parse_active: Callable[..., Any]
    permission_codes: Callable[..., Any]
    require_id: Callable[..., Any]
    ALL_PERMISSION_DEFINITIONS: Any
    AREA_LABELS: Any
    MODULE_LABELS: Any


def bind_routes(app, *, dependencies: Dependencies, runtime: RuntimeBindings, current_principal):
    def serialize_department(item: Department) -> dict[str, Any]:
        return {
            "id": item.id, "name": item.name, "parentId": item.parent_id, "active": item.active,
            "status": dependencies.legacy_status(item.active), "code": "", "description": getattr(item, "description", "") or "",
            "createTime": item.created_at.isoformat() if item.created_at else None, "children": [],
        }


    def serialize_position(item: Position) -> dict[str, Any]:
        return {
            "id": item.id, "name": item.name, "description": item.description, "active": item.active,
            "status": dependencies.legacy_status(item.active), "code": "",
            "createTime": item.created_at.isoformat() if item.created_at else None, "children": [],
        }


    def serialize_tenant(item: Tenant) -> dict[str, Any]:
        created = item.created_at.isoformat() if item.created_at else None
        return {
            "id": item.id, "code": item.code, "name": item.name, "active": item.active,
            "status": dependencies.legacy_status(item.active), "createdAt": created, "createTime": created,
        }


    def serialize_role(item: Role) -> dict[str, Any]:
        return {
            "id": item.id, "name": item.name, "description": item.description, "remarks": item.description,
            "tenantId": item.tenant_id, "createTime": None,
        }


    def permission_module_key(item: Permission) -> str:
        if item.code.startswith("Menu/"):
            parts = [part for part in item.code.split("/") if part]
            return parts[1] if len(parts) > 1 else "menu"
        parts = [part for part in item.code.split(".") if part]
        if len(parts) >= 3:
            return parts[1]
        if len(parts) == 2:
            return parts[0]
        return item.category or "api"


    def serialize_permission(item: Permission) -> dict[str, Any]:
        parts = [part for part in item.code.split(".") if part]
        category = item.category or "api"
        module = permission_module_key(item)
        action = parts[-1] if parts else item.code
        return {
            "id": item.id, "code": item.code, "name": item.name, "category": category,
            "fullName": item.name, "areaName": dependencies.AREA_LABELS.get(category, category), "area": category,
            "moduleName": dependencies.MODULE_LABELS.get(module, module), "module": module, "actionName": item.name, "action": action,
            "httpMethod": "", "isManual": False, "permissionType": 4, "permissionTypeName": "接口权限",
        }


    def serialize_area_permissions(permissions: list[Permission], assigned: set[str], role_id: int | None = None) -> dict[str, Any]:
        type_groups: dict[str, dict[str, Any]] = {}
        for item in permissions:
            serialized = serialize_permission(item)
            permission_type = "菜单权限" if (item.category == "menu" or item.code.startswith("Menu/")) else "接口权限"
            area = str(serialized.get("area") or serialized.get("category") or "api")
            module = str(serialized.get("module") or area)
            type_group = type_groups.setdefault(permission_type, {"permissionType": permission_type, "dtos": []})
            area_list = type_group["dtos"]
            area_node = next((row for row in area_list if row["area"] == area), None)
            if area_node is None:
                area_node = {
                    "area": area,
                    "areaName": dependencies.AREA_LABELS.get(area, str(serialized.get("areaName") or area)),
                    "modules": [],
                }
                area_list.append(area_node)
            module_list = area_node["modules"]
            module_node = next((row for row in module_list if row["module"] == module), None)
            if module_node is None:
                module_name = str(serialized.get("moduleName") or dependencies.MODULE_LABELS.get(module, module))
                module_node = {
                    "module": module,
                    "Module": module,
                    "moduleName": module_name,
                    "ModuleName": module_name,
                    "actions": [],
                }
                module_list.append(module_node)
            module_node["actions"].append({
                "id": item.id,
                "actionName": item.name,
                "action": serialized.get("action"),
                "fullName": item.name,
                "httpMethod": "",
                "isManual": False,
                "isPermission": item.id in assigned,
            })
        return {
            "roleId": str(role_id) if role_id is not None else "",
            "permissionIds": [item.id for item in permissions if item.id in assigned],
            "dtos": list(type_groups.values()),
        }


    @app.post("/api/User/RegistrationCodes")
    async def create_registration_code(
        response: Response, principal: Principal = Depends(current_principal),
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return await registration_codes.create(principal, session)


    @app.get("/api/User/RegistrationCodes")
    async def list_registration_codes(
        response: Response, limit: int = Query(default=50, ge=1, le=200),
        principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session),
    ) -> list[dict[str, Any]]:
        response.headers["Cache-Control"] = "no-store"
        return await registration_codes.listing(principal, session, limit)


    @app.delete("/api/User/RegistrationCodes/{code_id}")
    async def revoke_registration_code(
        code_id: str, principal: Principal = Depends(current_principal),
        session: AsyncSession = Depends(get_session),
    ) -> bool:
        return await registration_codes.revoke(code_id, principal, session)


    async def user_relations(users: list[User], session: AsyncSession):
        roles = {user.id: [] for user in users}
        if not users:
            return roles, {}, {}
        tenant_ids = {user.tenant_id for user in users}
        assignments = await session.execute(select(UserRole.user_id, Role.id, Role.name).join(
            Role, Role.id == UserRole.role_id).where(
                UserRole.user_id.in_(roles), Role.tenant_id.in_(tenant_ids)).order_by(Role.id))
        for user_id, role_id, name in assignments:
            roles[user_id].append({"id": role_id, "name": name})
        department_ids = {user.department_id for user in users if user.department_id}
        position_ids = {user.position_id for user in users if user.position_id}
        departments = {item.id: item for item in await session.scalars(select(Department).where(
            Department.id.in_(department_ids), Department.tenant_id.in_(tenant_ids)))} if department_ids else {}
        positions = {item.id: item for item in await session.scalars(select(Position).where(
            Position.id.in_(position_ids), Position.tenant_id.in_(tenant_ids)))} if position_ids else {}
        return roles, departments, positions

    async def serialize_user(user: User, session: AsyncSession, *, relations=None) -> dict[str, Any]:
        roles_by_user, departments, positions = relations if relations is not None else await user_relations([user], session)
        roles = roles_by_user.get(user.id, [])
        department = departments.get(user.department_id)
        position = positions.get(user.position_id)
        profile = dict(getattr(user, "profile", None) or {})
        created = user.created_at.isoformat() if user.created_at else None
        avatar = getattr(user, "avatar_url", None) or ""
        return {
            "id": user.id, "username": user.username, "name": user.username, "nickname": user.nickname,
            "nickName": user.nickname, "tenantId": user.tenant_id, "isActive": user.is_active,
            "status": 1 if user.is_active else 0, "isSuperAdmin": user.is_super_admin,
            "createdAt": created, "createTime": created,
            "phone": getattr(user, "phone", "") or "", "email": getattr(user, "email", "") or "",
            "avatar": avatar, "avatarUrl": avatar,
            "headImgs": [{"key": "avatar", "value": avatar}] if avatar else [],
            "roles": roles,
            "approvalStatus": getattr(user, "approval_status", "approved") or "approved",
            "positions": [{"id": position.id, "name": position.name}] if position else [],
            "departmentId": getattr(user, "department_id", None),
            "positionId": getattr(user, "position_id", None),
            "dtoUserInfo": {
                "departmentId": getattr(user, "department_id", None) or "",
                "departmentName": department.name if department else "",
                "employeeStatus": profile.get("employeeStatus", 1),
                "hireDate": profile.get("hireDate"),
                "signature": profile.get("signature", ""),
                "sex": profile.get("sex"),
                "supervisorId": profile.get("supervisorId"),
                "employeeNo": profile.get("employeeNo", ""),
                "weChat": profile.get("weChat", ""),
                "qq": profile.get("qq", ""),
                "id": profile.get("id", 0),
            },
        }


    async def validate_organization_reference(
        model: type[Department] | type[Position],
        value: Any,
        principal: Principal,
        session: AsyncSession,
    ) -> int | None:
        if isinstance(value, bool):
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "组织引用 ID 无效"})
        if value in (None, "", 0, "0"):
            return None
        item_id = dependencies.require_id(value, "组织引用 ID 无效")
        if not 0 < item_id < 2**63:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "组织引用 ID 无效"})
        item = await session.get(model, item_id)
        if item is None or item.tenant_id != principal.tenant_id or not item.active:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "组织引用不属于当前租户或已停用"})
        return item_id


    async def persist_user_profile(user: User, payload: dict[str, Any], principal: Principal, session: AsyncSession) -> None:
        if "phone" in payload:
            user.phone = str(payload.get("phone") or "")[:32]
        if "email" in payload:
            user.email = str(payload.get("email") or "")[:160]
        dto = payload.get("dtoUserInfo") if isinstance(payload.get("dtoUserInfo"), dict) else {}
        if "departmentId" in payload or "departmentId" in dto:
            department_ids = [
                await validate_organization_reference(Department, source["departmentId"], principal, session)
                for source in (payload, dto) if "departmentId" in source
            ]
            if len(set(department_ids)) != 1:
                raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "部门别名字段不一致"})
            user.department_id = department_ids[0]
        if "positions" in payload or "positionId" in payload:
            positions = payload.get("positions", [])
            if positions is None:
                positions = []
            if not isinstance(positions, list):
                raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "岗位列表格式无效"})
            position_id = None
            if isinstance(positions, list) and positions:
                validated = [
                    await validate_organization_reference(Position, entry.get("id") if isinstance(entry, dict) else entry, principal, session)
                    for entry in positions
                ]
                position_id = validated[0]
            if position_id is None:
                position_id = await validate_organization_reference(Position, payload.get("positionId"), principal, session)
            elif "positionId" in payload:
                explicit_position_id = await validate_organization_reference(Position, payload["positionId"], principal, session)
                if explicit_position_id != position_id:
                    raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "岗位别名字段不一致"})
            user.position_id = position_id
        avatar_url = str(payload.get("avatarUrl") or payload.get("avatar") or "")
        head_imgs = payload.get("headImgs") or []
        has_avatar = "avatarUrl" in payload or "avatar" in payload or "headImgs" in payload
        if isinstance(head_imgs, list) and head_imgs:
            first = head_imgs[0]
            if isinstance(first, dict):
                avatar_url = str(first.get("value") or first.get("url") or avatar_url)
            elif first:
                avatar_url = str(first)
        if has_avatar and avatar_url.startswith("data:image"):
            from eic.core.avatars import save_avatar
            avatar_principal = Principal(user.id, user.tenant_id, user.username, user.is_super_admin)
            avatar_url = await save_avatar(avatar_url, avatar_principal, session, runtime.upload_root,
                                           runtime.settings, runtime.sessions, dependencies.lock_tenant_writes)
        if has_avatar and avatar_url:
            user.avatar_url = avatar_url[:4000]
        if dto:
            hire_date = dto.get("hireDate")
            if hasattr(hire_date, "isoformat"):
                hire_date = hire_date.isoformat()
            merged = dict(getattr(user, "profile", None) or {})
            merged.update({
                "employeeStatus": dto.get("employeeStatus", merged.get("employeeStatus", 1)),
                "hireDate": str(hire_date or merged.get("hireDate") or "")[:80],
                "signature": str(dto.get("signature") if "signature" in dto else merged.get("signature") or "")[:200],
                "sex": dto.get("sex") if "sex" in dto else merged.get("sex"),
                "supervisorId": dto.get("supervisorId") if "supervisorId" in dto else merged.get("supervisorId"),
                "employeeNo": str(dto.get("employeeNo") if "employeeNo" in dto else merged.get("employeeNo") or "")[:80],
                "weChat": str(dto.get("weChat") if "weChat" in dto else merged.get("weChat") or "")[:80],
                "qq": str(dto.get("qq") if "qq" in dto else merged.get("qq") or "")[:40],
                "id": dto.get("id") or merged.get("id") or 0,
            })
            user.profile = merged


    @app.get("/api/User/GetUser")
    async def get_user(principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        user = await session.get(User, principal.user_id)
        if user is None:
            return {"id": principal.user_id, "name": principal.username, "tenantId": principal.tenant_id, "isSuperAdmin": principal.is_super_admin}
        data = await serialize_user(user, session)
        data.update({"name": user.username, "tenantId": principal.tenant_id, "isSuperAdmin": principal.is_super_admin})
        return data


    @app.get("/api/base/GetSnowflakeId")
    @app.get("/api/Base/GetSnowflakeId")
    async def get_snowflake_id(_: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> int:
        value = await dependencies.new_id(session)
        await session.commit()
        return value


    @app.get("/api/base/GetGuId")
    @app.get("/api/Base/GetGuId")
    async def get_guid(_: Principal = Depends(current_principal)) -> str:
        return str(uuid.uuid4())


    @app.get("/api/Permission/GetUserPermissions")
    async def get_user_permissions(principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> list[str]:
        return sorted(await dependencies.permission_codes(principal, session))


    @app.post("/api/User/GetSysUserList")
    @app.post("/api/User/GetPageData")
    async def user_page(payload: PageInput, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        await dependencies.ensure_permission("system.user.read", principal, session)
        criteria = [User.tenant_id == principal.tenant_id]
        if payload.searchKey:
            criteria.append(User.username.contains(payload.searchKey))
        total = await session.scalar(select(func.count()).select_from(User).where(*criteria)) or 0
        users = list(await session.scalars(select(User).where(*criteria).order_by(User.created_at.desc()).offset((payload.pageNum - 1) * payload.pageSize).limit(payload.pageSize)))
        relations = await user_relations(users, session)
        return dependencies.page_result([await serialize_user(user, session, relations=relations) for user in users], payload, total)


    @app.post("/api/User/AddEdit")
    @app.post("/api/User/EditUser")
    async def user_add_edit(payload: UserWrite, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        # 在修改资料前开始写事务；头像复用此锁，不能再次 rollback 丢弃资料或预占。
        payload = write_payload(payload, UserWrite)
        await dependencies.lock_tenant_writes(session, principal.tenant_id)
        await dependencies.new_id(session)
        session.info["profileUploadTransaction"] = (principal.tenant_id, session.sync_session.get_transaction())
        user_id = int(payload["id"]) if payload.get("id") else None
        is_self = user_id is not None and user_id == principal.user_id
        if not is_self:
            await dependencies.ensure_permission("system.user.write", principal, session)
        else:
            codes = await dependencies.permission_codes(principal, session)
            if "system.user.write" not in codes and not principal.is_super_admin:
                for key in ("username", "name", "isActive", "status", "roleIds", "roleId", "roles", "password", "passWord", "PassWord"):
                    payload.pop(key, None)
        user = await session.scalar(select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)) if user_id else None
        if user is not None and user.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "用户不存在"})
        username = str(payload.get("username") or payload.get("name") or "").strip()
        nickname = str(payload.get("nickname") or payload.get("nickName") or "")
        password = str(payload.get("password") or payload.get("passWord") or payload.get("PassWord") or "")
        if user is None:
            if not username or not password:
                raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "新增用户必须提供账号和密码"})
            user = User(id=await dependencies.new_entity_id(session, user_id), tenant_id=principal.tenant_id, username=username, nickname=nickname, password_hash=hash_password(password), is_active=True, approval_status="approved")
            session.add(user)
        else:
            if username:
                user.username = username
            if nickname:
                user.nickname = nickname
            if password:
                user.password_hash = hash_password(password)
        if "isActive" in payload or "status" in payload:
            user.is_active = dependencies.parse_active({"active": payload.get("isActive", payload.get("status"))}, user.is_active)
        has_roles = "roleIds" in payload or "roleId" in payload or "roles" in payload
        if has_roles:
            if "roleIds" in payload or "roleId" in payload:
                raw_role_ids = payload.get("roleIds", [payload.get("roleId")])
                if not isinstance(raw_role_ids, list):
                    raw_role_ids = [raw_role_ids]
                role_ids = {int(value) for value in raw_role_ids if value is not None}
            else:
                role_ids = set()
                for item in payload.get("roles") or []:
                    value = item.get("id") if isinstance(item, dict) else item
                    if value is not None:
                        role_ids.add(int(value))
            valid_role_ids = set(await session.scalars(select(Role.id).where(Role.id.in_(role_ids), Role.tenant_id == principal.tenant_id))) if role_ids else set()
            if valid_role_ids != role_ids:
                raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "角色不属于当前租户"})
            await session.execute(delete(UserRole).where(UserRole.user_id == user.id))
            session.add_all(UserRole(user_id=user.id, role_id=role_id) for role_id in valid_role_ids)
        from eic.core.avatars import compensate_profile_uploads, retire_previous_avatar
        previous_avatar = user.avatar_url
        try:
            await persist_user_profile(user, payload, principal, session)
            await session.commit()
            session.info.pop("stagedUploads", None)
            session.info.pop("profileUploadTransaction", None)
        except BaseException:
            await complete_cleanup(compensate_profile_uploads(session, runtime.sessions, dependencies.lock_tenant_writes))
            raise
        if previous_avatar != user.avatar_url:
            await retire_previous_avatar(previous_avatar, user.id, user.tenant_id, runtime.upload_root, runtime.sessions, dependencies.lock_tenant_writes)
        return True


    @app.delete("/api/User/Delete")
    async def user_delete(Id: int, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        await dependencies.ensure_permission("system.user.write", principal, session)
        user = await session.get(User, Id)
        if user is None or user.tenant_id != principal.tenant_id or user.id == principal.user_id:
            return False
        user.is_active = False
        await session.commit()
        return True


    @app.delete("/api/User/DeleteUser")
    async def user_delete_legacy(id: int, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        return await user_delete(id, principal, session)


    @app.get("/api/User/GetUserRoleKey")
    async def user_role_key(principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
        await dependencies.ensure_permission("system.user.read", principal, session)
        roles = await session.scalars(select(Role).where(Role.tenant_id == principal.tenant_id).order_by(Role.name))
        return [{"key": role.id, "value": role.name} for role in roles]


    @app.get("/api/User/GetAllUserCount")
    async def user_count(principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, int]:
        if not await dependencies.has_permission("system.user.read", principal, session):
            return {"count": 0, "total": 0}
        count = await session.scalar(select(func.count()).select_from(User).where(User.tenant_id == principal.tenant_id)) or 0
        return {"count": count, "total": count}


    @app.get("/api/User/GetPendingApprovals")
    async def pending_approvals(principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
        await dependencies.ensure_permission("system.user.read", principal, session)
        users = await session.scalars(
            select(User).where(User.tenant_id == principal.tenant_id, User.approval_status == "pending").order_by(User.created_at.desc())
        )
        return [await serialize_user(user, session) for user in users]


    @app.post("/api/User/ApproveRegistration")
    async def approve_registration(payload: dict[str, Any], principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        await dependencies.ensure_permission("system.user.write", principal, session)
        user_id = dependencies.first_int(payload.get("userId"), payload.get("id"))
        user = await session.get(User, user_id) if user_id else None
        if user is None or user.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "用户不存在"})
        role_id = dependencies.first_int(payload.get("roleId"))
        if role_id is None:
            standard = await session.scalar(select(Role).where(Role.tenant_id == principal.tenant_id, Role.name == "普通用户"))
            role_id = standard.id if standard is not None else None
        if role_id is not None:
            role = await session.get(Role, role_id)
            if role is None or role.tenant_id != principal.tenant_id:
                raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "角色不属于当前租户"})
            has_role = await session.scalar(select(UserRole).where(UserRole.user_id == user.id, UserRole.role_id == role.id))
            if has_role is None:
                session.add(UserRole(user_id=user.id, role_id=role.id))
        extra_ids = payload.get("permissionIds") or payload.get("permissions") or []
        extra_codes = {str(value.get("id") if isinstance(value, dict) else value) for value in extra_ids if value}
        extra_codes = {code for code in extra_codes if code}
        if extra_codes:
            valid_ids = set(await session.scalars(select(Permission.id).where(Permission.id.in_(extra_codes))))
            if valid_ids != extra_codes:
                raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "存在无效权限"})
            extra_role = await session.scalar(select(Role).where(Role.tenant_id == principal.tenant_id, Role.name == f"附加权限-{user.username}"))
            if extra_role is None:
                extra_role = Role(id=await dependencies.new_id(session), tenant_id=principal.tenant_id, name=f"附加权限-{user.username}", description="审批时单独勾选的权限")
                session.add(extra_role)
                await session.flush()
            assigned = set(await session.scalars(select(RolePermission.permission_id).where(RolePermission.role_id == extra_role.id)))
            session.add_all(RolePermission(role_id=extra_role.id, permission_id=code) for code in extra_codes if code not in assigned)
            has_extra = await session.scalar(select(UserRole).where(UserRole.user_id == user.id, UserRole.role_id == extra_role.id))
            if has_extra is None:
                session.add(UserRole(user_id=user.id, role_id=extra_role.id))
        if "departmentId" in payload:
            user.department_id = await validate_organization_reference(Department, payload["departmentId"], principal, session)
        if "positionId" in payload:
            user.position_id = await validate_organization_reference(Position, payload["positionId"], principal, session)
        user.approval_status = "approved"
        user.is_active = True
        await session.commit()
        return True


    @app.post("/api/User/RejectRegistration")
    async def reject_registration(payload: dict[str, Any], principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        await dependencies.ensure_permission("system.user.write", principal, session)
        user_id = dependencies.first_int(payload.get("userId"), payload.get("id"))
        user = await session.get(User, user_id) if user_id else None
        if user is None or user.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "用户不存在"})
        user.approval_status = "rejected"
        user.is_active = False
        await session.commit()
        return True


    @app.post("/api/Role/GetRolePage")
    @app.post("/api/Role/GetPageData")
    async def role_page(payload: PageInput, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        await dependencies.ensure_permission("system.role.read", principal, session)
        criteria = [Role.tenant_id == principal.tenant_id]
        if payload.searchKey:
            criteria.append(Role.name.contains(payload.searchKey))
        total = await session.scalar(select(func.count()).select_from(Role).where(*criteria)) or 0
        roles = await session.scalars(select(Role).where(*criteria).order_by(Role.id.desc()).offset((payload.pageNum - 1) * payload.pageSize).limit(payload.pageSize))
        return dependencies.page_result([serialize_role(role) for role in roles], payload, total)


    @app.post("/api/Role/AddEdit")
    @app.post("/api/Role/AddEidt")
    async def role_add_edit(payload: RoleWrite, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        payload = write_payload(payload, RoleWrite)
        await dependencies.ensure_permission("system.role.write", principal, session)
        role = await session.get(Role, int(payload["id"])) if payload.get("id") else None
        if role is not None and role.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "角色不存在"})
        remarks = payload.get("remarks")
        description = payload.get("description")
        saved_description = str(remarks if remarks is not None else description or "")
        if role is None:
            role = Role(id=await dependencies.new_id(session), tenant_id=principal.tenant_id, name=str(payload.get("name") or ""), description=saved_description)
            session.add(role)
        else:
            role.name = str(payload.get("name") or role.name)
            if remarks is not None or description is not None:
                role.description = saved_description
        await session.commit()
        return True


    @app.delete("/api/Role/Delete")
    async def role_delete(Id: int, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        await dependencies.ensure_permission("system.role.write", principal, session)
        role = await session.get(Role, Id)
        if role is None or role.tenant_id != principal.tenant_id:
            return False
        await session.execute(delete(UserRole).where(UserRole.role_id == role.id))
        await session.execute(delete(RolePermission).where(RolePermission.role_id == role.id))
        await session.delete(role)
        await session.commit()
        return True


    @app.delete("/api/Role/deleteRole")
    async def role_delete_legacy(roleId: int, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        return await role_delete(roleId, principal, session)


    @app.get("/api/Role/GetRoleById")
    async def role_details(roleId: int, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        await dependencies.ensure_permission("system.role.read", principal, session)
        role = await session.get(Role, roleId)
        if role is None or role.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "角色不存在"})
        permission_ids = list(await session.scalars(select(RolePermission.permission_id).where(RolePermission.role_id == role.id)))
        return {"id": role.id, "name": role.name, "description": role.description, "permissionIds": permission_ids}


    @app.get("/api/Role/GetAllRoles")
    async def role_all(principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
        await dependencies.ensure_permission("system.role.read", principal, session)
        roles = await session.scalars(select(Role).where(Role.tenant_id == principal.tenant_id).order_by(Role.name))
        return [serialize_role(item) for item in roles]


    @app.get("/api/Permission/GetDetails")
    async def permission_details(roleId: str | None = None, Id: str | None = None, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        await dependencies.ensure_permission("system.permission.read", principal, session)
        raw = str(roleId or Id or "").strip()
        if raw and not raw.isdigit():
            item = await session.get(Permission, raw)
            if item is None:
                item = await session.scalar(select(Permission).where(Permission.code == raw))
            if item is None:
                raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "权限不存在"})
            return serialize_permission(item)
        role = await session.get(Role, int(raw or 0))
        if role is None or role.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "角色不存在"})
        ids = list(await session.scalars(select(RolePermission.permission_id).where(RolePermission.role_id == role.id)))
        return {"roleId": role.id, "permissionIds": ids}


    @app.get("/api/Permission/GetAllAreaPermissions")
    async def permission_for_role(roleId: int, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        await dependencies.ensure_permission("system.permission.read", principal, session)
        role = await session.get(Role, roleId)
        if role is None or role.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "角色不存在"})
        permissions = await session.scalars(select(Permission).order_by(Permission.category, Permission.code))
        assigned = set(await session.scalars(select(RolePermission.permission_id).where(RolePermission.role_id == role.id)))
        return serialize_area_permissions(list(permissions), assigned, role.id)


    @app.post("/api/Permission/GetPageData")
    async def permission_page(payload: PageInput, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        await dependencies.ensure_permission("system.permission.read", principal, session)
        criteria = []
        if payload.searchKey:
            criteria.append(Permission.name.contains(payload.searchKey) | Permission.code.contains(payload.searchKey))
        total = await session.scalar(select(func.count()).select_from(Permission).where(*criteria)) or 0
        items = await session.scalars(select(Permission).where(*criteria).order_by(Permission.category, Permission.code).offset((payload.pageNum - 1) * payload.pageSize).limit(payload.pageSize))
        return dependencies.page_result([serialize_permission(p) for p in items], payload, total)


    @app.post("/api/Permission/EditAllAreaPermissions")
    async def permission_assign(payload: PermissionWrite, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        payload = write_payload(payload, PermissionWrite)
        await dependencies.ensure_permission("system.permission.write", principal, session)
        role_id = int(payload.get("roleId") or payload.get("id"))
        role = await session.get(Role, role_id)
        if role is None or role.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "角色不存在"})
        raw_ids = payload.get("permissionIds", payload.get("permissions", []))
        permission_ids = {str(value.get("id") if isinstance(value, dict) else value) for value in raw_ids}
        valid_ids = set(await session.scalars(select(Permission.id).where(Permission.id.in_(permission_ids)))) if permission_ids else set()
        if valid_ids != permission_ids:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "存在无效权限"})
        await session.execute(delete(RolePermission).where(RolePermission.role_id == role.id))
        session.add_all(RolePermission(role_id=role.id, permission_id=permission_id) for permission_id in valid_ids)
        await session.commit()
        return True


    @app.post("/api/Permission/Edit")
    async def permission_edit(payload: PermissionWrite, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        payload = write_payload(payload, PermissionWrite)
        await dependencies.ensure_permission("system.permission.write", principal, session)
        code = str(payload.get("code") or payload.get("id") or "").strip()
        if not code:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "权限编码不能为空"})
        item = await session.get(Permission, code)
        if item is None:
            item = Permission(id=code, code=code, name=str(payload.get("name") or code), category=str(payload.get("category") or "api"))
            session.add(item)
        else:
            item.name = str(payload.get("name") or item.name)
            item.category = str(payload.get("category") or item.category)
        await session.commit()
        return True


    @app.delete("/api/Permission/Del")
    async def permission_delete(roleId: int | None = None, Id: int | None = None, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        roleId = roleId or Id or 0
        await dependencies.ensure_permission("system.permission.write", principal, session)
        role = await session.get(Role, roleId)
        if role is None or role.tenant_id != principal.tenant_id:
            return False
        await session.execute(delete(RolePermission).where(RolePermission.role_id == role.id))
        await session.commit()
        return True


    @app.post("/api/Permission/Reload")
    @app.get("/api/Permission/Reload")
    async def permission_reload(principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        await dependencies.ensure_permission("system.permission.write", principal, session)
        existing = set(await session.scalars(select(Permission.id)))
        added = 0
        for code, name, category in dependencies.ALL_PERMISSION_DEFINITIONS:
            if code not in existing:
                session.add(Permission(id=code, code=code, name=name, category=category))
                added += 1
        await session.commit()
        return {"added": added, "total": len(existing) + added}


    @app.get("/api/Permission/GetAllList")
    async def permission_list(principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
        await dependencies.ensure_permission("system.permission.read", principal, session)
        permissions = await session.scalars(select(Permission).order_by(Permission.category, Permission.code))
        return [serialize_permission(item) for item in permissions]


    @app.get("/api/Department/GetAllList")
    @app.get("/api/Department/GetALLList")
    async def department_list(principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
        items = await session.scalars(select(Department).where(Department.tenant_id == principal.tenant_id, Department.active.is_(True)).order_by(Department.name))
        return [serialize_department(item) for item in items]


    @app.get("/api/Position/GetAllList")
    @app.get("/api/Position/GetALLList")
    async def position_list(principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
        items = await session.scalars(select(Position).where(Position.tenant_id == principal.tenant_id, Position.active.is_(True)).order_by(Position.name))
        return [serialize_position(item) for item in items]


    def require_super_admin(principal: Principal) -> None:
        if not principal.is_super_admin:
            raise HTTPException(status_code=403, detail={"code": 403, "errMsg": "仅系统管理员可执行此操作"})


    @app.post("/api/Department/GetPageData")
    async def department_page(payload: PageInput, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        await dependencies.ensure_permission("system.department.read", principal, session)
        criteria = [Department.tenant_id == principal.tenant_id, Department.active.is_(True)]
        if payload.searchKey:
            criteria.append(Department.name.contains(payload.searchKey))
        total = await session.scalar(select(func.count()).select_from(Department).where(*criteria)) or 0
        items = await session.scalars(select(Department).where(*criteria).order_by(Department.name).offset((payload.pageNum - 1) * payload.pageSize).limit(payload.pageSize))
        return dependencies.page_result([serialize_department(x) for x in items], payload, total)


    @app.post("/api/Department/AddEdit")
    async def department_add_edit(payload: DepartmentWrite, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        payload = write_payload(payload, DepartmentWrite)
        await dependencies.ensure_permission("system.department.write", principal, session)
        await dependencies.lock_tenant_writes(session, principal.tenant_id)
        item = await session.get(Department, int(payload["id"])) if payload.get("id") else None
        if payload.get("id") and item is None:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "部门不存在"})
        if item is not None and item.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "部门不存在"})
        name = str(payload.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "部门名称不能为空"})
        parent_id = int(payload["parentId"]) if payload.get("parentId") not in (None, "", 0, "0") else None
        await validate_parent(session, principal.tenant_id, item.id if item else None, parent_id)
        next_active = dependencies.parse_active(payload, item.active if item else True)
        if item is not None and not next_active:
            children = await session.scalar(select(Department.id).where(
                Department.tenant_id == principal.tenant_id, Department.parent_id == item.id,
                Department.active.is_(True),
            ).limit(1))
            if children is not None:
                raise HTTPException(status_code=409, detail={"code": 409, "errMsg": "部门仍有启用的下级，请先显式调整下级部门"})
        description = str(payload.get("description") or payload.get("describe") or "")[:500]
        if item is None:
            item = Department(id=await dependencies.new_id(session), tenant_id=principal.tenant_id, name=name, parent_id=parent_id, active=dependencies.parse_active(payload, True), description=description)
            session.add(item)
        else:
            item.name, item.parent_id, item.active = name, parent_id, dependencies.parse_active(payload, item.active)
            item.description = description
        await session.commit()
        return True


    @app.delete("/api/Department/Delete")
    async def department_delete(Id: int, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        await dependencies.ensure_permission("system.department.write", principal, session)
        await dependencies.lock_tenant_writes(session, principal.tenant_id)
        item = await session.get(Department, Id)
        if item is None or item.tenant_id != principal.tenant_id:
            return False
        children = await session.scalar(select(Department.id).where(
            Department.tenant_id == principal.tenant_id, Department.parent_id == item.id,
            Department.active.is_(True),
        ).limit(1))
        if children is not None:
            raise HTTPException(status_code=409, detail={"code": 409, "errMsg": "部门仍有启用的下级，请先显式调整下级部门"})
        item.active = False
        await session.commit()
        return True


    @app.get("/api/Department/GetDepartmentTree")
    async def department_tree(principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
        await dependencies.ensure_permission("system.department.read", principal, session)
        items = list(await session.scalars(select(Department).where(Department.tenant_id == principal.tenant_id, Department.active.is_(True)).order_by(Department.name)))
        cycles = cycle_members({item.id: item.parent_id for item in items})
        if cycles:
            raise HTTPException(status_code=409, detail={
                "code": 409, "errMsg": "既有部门层级存在环，已保留原数据，请先核验",
                "departmentIds": [str(item_id) for item_id in sorted(cycles)],
            })
        nodes = {item.id: serialize_department(item) for item in items}
        roots = []
        for item in items:
            node = nodes[item.id]
            if item.parent_id in nodes:
                nodes[item.parent_id]["children"].append(node)
            else:
                roots.append(node)
        return roots


    @app.post("/api/Position/GetPageData")
    async def position_page(payload: PageInput, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        await dependencies.ensure_permission("system.position.read", principal, session)
        criteria = [Position.tenant_id == principal.tenant_id, Position.active.is_(True)]
        if payload.searchKey:
            criteria.append(Position.name.contains(payload.searchKey))
        total = await session.scalar(select(func.count()).select_from(Position).where(*criteria)) or 0
        items = await session.scalars(select(Position).where(*criteria).order_by(Position.name).offset((payload.pageNum - 1) * payload.pageSize).limit(payload.pageSize))
        return dependencies.page_result([serialize_position(x) for x in items], payload, total)


    @app.post("/api/Position/AddEdit")
    async def position_add_edit(payload: PositionWrite, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        payload = write_payload(payload, PositionWrite)
        await dependencies.ensure_permission("system.position.write", principal, session)
        item = await session.get(Position, int(payload["id"])) if payload.get("id") else None
        if item is not None and item.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "岗位不存在"})
        name = str(payload.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "岗位名称不能为空"})
        if item is None:
            item = Position(id=await dependencies.new_id(session), tenant_id=principal.tenant_id, name=name, description=str(payload.get("description") or ""), active=dependencies.parse_active(payload, True))
            session.add(item)
        else:
            item.name, item.description, item.active = name, str(payload.get("description") or item.description), dependencies.parse_active(payload, item.active)
        await session.commit()
        return True


    @app.delete("/api/Position/Delete")
    async def position_delete(Id: int, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        await dependencies.ensure_permission("system.position.write", principal, session)
        item = await session.get(Position, Id)
        if item is None or item.tenant_id != principal.tenant_id:
            return False
        item.active = False
        await session.commit()
        return True


    @app.get("/api/Position/GetPositionTree")
    async def position_tree(principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
        await dependencies.ensure_permission("system.position.read", principal, session)
        items = await session.scalars(select(Position).where(Position.tenant_id == principal.tenant_id, Position.active.is_(True)).order_by(Position.name))
        return [serialize_position(x) for x in items]


    @app.post("/api/Tenant/GetPageData")
    async def tenant_page(payload: PageInput, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        await dependencies.ensure_permission("system.tenant.read", principal, session)
        criteria = [] if principal.is_super_admin else [Tenant.id == principal.tenant_id]
        if payload.searchKey:
            criteria.append(Tenant.name.contains(payload.searchKey))
        total = await session.scalar(select(func.count()).select_from(Tenant).where(*criteria)) or 0
        items = await session.scalars(select(Tenant).where(*criteria).order_by(Tenant.id).offset((payload.pageNum - 1) * payload.pageSize).limit(payload.pageSize))
        return dependencies.page_result([serialize_tenant(x) for x in items], payload, total)


    @app.post("/api/Tenant/AddEdit")
    async def tenant_add_edit(payload: TenantWrite, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        payload = write_payload(payload, TenantWrite)
        await dependencies.ensure_permission("system.tenant.write", principal, session)
        require_super_admin(principal)
        item = await session.get(Tenant, int(payload["id"])) if payload.get("id") else None
        code = int(payload.get("code") or 0)
        name = str(payload.get("name") or "").strip()
        if item is None:
            if not name or code <= 0:
                raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "租户编码和名称不能为空"})
            item = Tenant(id=await dependencies.new_id(session), code=code, name=name, active=dependencies.parse_active(payload, True))
            session.add(item)
        else:
            if name:
                item.name = name
            if code > 0:
                item.code = code
            if "active" in payload or "status" in payload:
                item.active = dependencies.parse_active(payload, item.active)
        await session.commit()
        return True


    @app.delete("/api/Tenant/Delete")
    async def tenant_delete(Id: int, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        await dependencies.ensure_permission("system.tenant.write", principal, session)
        require_super_admin(principal)
        item = await session.get(Tenant, Id)
        if item is None:
            return False
        item.active = False
        await session.commit()
        return True


    @app.post("/api/User/ChangePasswordTokenUser")
    async def change_password(payload: dict[str, Any], principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        user = await session.get(User, principal.user_id)
        def password_field(keys: tuple[str, ...], label: str) -> str:
            values = [payload[key] for key in keys if key in payload]
            if not values or any(not isinstance(value, str) or not value or len(value) > 1024 for value in values):
                raise HTTPException(status_code=400, detail={"code": 400, "errMsg": f"{label}字段缺失或格式无效"})
            if any(value != values[0] for value in values):
                raise HTTPException(status_code=400, detail={"code": 400, "errMsg": f"{label}的兼容字段不一致"})
            return values[0]
        old_password = password_field(("oldPassword", "oldPassWord", "OldPwd"), "原密码")
        new_password = password_field(("newPassword", "NewPwd", "passWord", "password"), "新密码")
        if user is None or user.tenant_id != principal.tenant_id or not verify_password(old_password, user.password_hash):
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "原密码不正确"})
        if len(new_password) < 10:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "新密码至少需要 10 个字符"})
        user.password_hash = hash_password(new_password)
        await session.commit()
        return True

    return _RouteExports(
        serialize_department=serialize_department,
        serialize_position=serialize_position,
        serialize_tenant=serialize_tenant,
        serialize_role=serialize_role,
        permission_module_key=permission_module_key,
        serialize_permission=serialize_permission,
        serialize_area_permissions=serialize_area_permissions,
        create_registration_code=create_registration_code,
        list_registration_codes=list_registration_codes,
        revoke_registration_code=revoke_registration_code,
        serialize_user=serialize_user,
        validate_organization_reference=validate_organization_reference,
        persist_user_profile=persist_user_profile,
        get_user=get_user,
        get_snowflake_id=get_snowflake_id,
        get_guid=get_guid,
        get_user_permissions=get_user_permissions,
        user_page=user_page,
        user_add_edit=user_add_edit,
        user_delete=user_delete,
        user_delete_legacy=user_delete_legacy,
        user_role_key=user_role_key,
        user_count=user_count,
        pending_approvals=pending_approvals,
        approve_registration=approve_registration,
        reject_registration=reject_registration,
        role_page=role_page,
        role_add_edit=role_add_edit,
        role_delete=role_delete,
        role_delete_legacy=role_delete_legacy,
        role_details=role_details,
        role_all=role_all,
        permission_details=permission_details,
        permission_for_role=permission_for_role,
        permission_page=permission_page,
        permission_assign=permission_assign,
        permission_edit=permission_edit,
        permission_delete=permission_delete,
        permission_reload=permission_reload,
        permission_list=permission_list,
        department_list=department_list,
        position_list=position_list,
        require_super_admin=require_super_admin,
        department_page=department_page,
        department_add_edit=department_add_edit,
        department_delete=department_delete,
        department_tree=department_tree,
        position_page=position_page,
        position_add_edit=position_add_edit,
        position_delete=position_delete,
        position_tree=position_tree,
        tenant_page=tenant_page,
        tenant_add_edit=tenant_add_edit,
        tenant_delete=tenant_delete,
        change_password=change_password,
    )
