"""Current-state checks for new work, independent of historical visibility."""
from sqlalchemy.ext.asyncio import AsyncSession

from eic.models import AIModel, Resource


class RuntimeReferenceUnavailable(ValueError):
    """A configured reference no longer permits execution."""


async def require_runtime_resource(
    session: AsyncSession, tenant_id: int, item_id: int, resource_type: str,
    dedicated_class=None, *, require_dedicated: bool = False,
):
    resource = await session.get(Resource, item_id)
    dedicated = await session.get(dedicated_class, item_id) if dedicated_class else None
    if resource is not None and (
        resource.tenant_id != tenant_id or resource.resource_type != resource_type
        or resource.is_deleted or (resource.data or {}).get("enabled") is False
    ):
        raise RuntimeReferenceUnavailable("runtime_reference_unavailable")
    if dedicated is not None and (
        dedicated.tenant_id != tenant_id or not getattr(dedicated, "enabled", True)
    ):
        raise RuntimeReferenceUnavailable("runtime_reference_unavailable")
    if (resource is None and dedicated is None) or (require_dedicated and dedicated is None):
        raise RuntimeReferenceUnavailable("runtime_reference_unavailable")
    return resource, dedicated


async def require_runtime_model(session: AsyncSession, tenant_id: int, model_id: int, model_type: str):
    model = await session.get(AIModel, model_id)
    if model is None or model.tenant_id != tenant_id or model.model_type != model_type or not model.enabled:
        raise RuntimeReferenceUnavailable("runtime_reference_unavailable")
    return model
