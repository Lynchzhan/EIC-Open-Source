"""部门层级校验；写调用者必须先持有租户事务锁。"""
from fastapi import HTTPException
from sqlalchemy import select

from eic.models import Department


async def validate_parent(session, tenant_id: int, item_id: int | None, parent_id: int | None) -> None:
    seen = set()
    current = parent_id
    while current is not None:
        if current == item_id:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "部门上级不能指向自身或子孙部门"})
        if current in seen:
            raise HTTPException(status_code=409, detail={"code": 409, "errMsg": "既有部门层级存在环，请先核验历史数据"})
        seen.add(current)
        parent = await session.scalar(select(Department).where(
            Department.id == current, Department.tenant_id == tenant_id,
            Department.active.is_(True),
        ).execution_options(populate_existing=True))
        if parent is None:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "上级部门不存在或已停用"})
        current = parent.parent_id


def cycle_members(parents: dict[int, int | None]) -> set[int]:
    """迭代遍历，历史环只报告，不断边或修改记录。"""
    visited = set()
    cycles = set()
    for start in parents:
        path = []
        positions = {}
        current = start
        while current in parents and current not in visited:
            if current in positions:
                cycles.update(path[positions[current]:])
                break
            positions[current] = len(path)
            path.append(current)
            current = parents[current]
        visited.update(path)
    return cycles
