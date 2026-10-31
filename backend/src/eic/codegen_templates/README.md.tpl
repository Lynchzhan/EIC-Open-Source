# EIC 业务 CRUD 代码包

这是供人工审查与合入的代码包，不会自动写入源码、注册路由、分配权限或执行数据库迁移。
基于现有 Department、Position 模型和业务函数，不创建平行数据表，不导出任何业务记录。
目录中的每个实体包含输入校验、CRUD 路由、前端 API、Naive UI 管理页面。

## 手动接入

1. 审查后将包内 backend/frontend 文件合入对应目录，保留包目录的 __init__.py。
2. 在 eic/main.py 的业务函数定义之后、mount_frontend() 调用之前，按需绑定：

```python
from eic.generated.department.routes import bind as bind_department
bind_department(app, current_principal=current_principal, get_session=get_session, ensure_permission=ensure_permission,
    page_input=PageInput, list_records=department_page,
    save_record=department_add_edit, delete_record=department_delete)
from eic.generated.position.routes import bind as bind_position
bind_position(app, current_principal=current_principal, get_session=get_session, ensure_permission=ensure_permission,
    page_input=PageInput, list_records=position_page,
    save_record=position_add_edit, delete_record=position_delete)
# 只绑定本次选择的实体；不在代码包中的实体不要导入。
```

3. 在前端 router.js 手动导入 pages/generated/DepartmentManagement.vue 或 PositionManagement.vue，配置登录保护及现有菜单权限。生成模块使用 /api/Generated/Entity/*，避免覆盖现有接口。
4. 读写仍由 system.department.read/write 或 system.position.read/write 权限控制，租户身份由服务端会话提供，客户端不得指定。
5. 分页只显示启用记录；停用等同现有软删除，不会物理删除。部门必须满足现有父级与下级约束。父级 ID 留空表示顶级。
6. 本包未执行构建、接口测试或迁移；合入后另行安排验证。生成不等于已经部署。
