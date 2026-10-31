# 历史数据迁移

迁移工具默认只在显式提供连接串和输入清单时运行，不会从源码目录推断云端地址，也不会把生产数据复制进源码包。

## MySQL 业务数据

先做只读审计：

```powershell
python migration\scripts\audit_mysql_migration.py `
  --source-url "mysql+aiomysql://USER:PASSWORD@HOST:3306/SOURCE_DB" `
  --output migration\reports\mysql_migration_audit.json
```

先 dry-run：

```powershell
python migration\scripts\migrate_mysql.py `
  --source-url "mysql+aiomysql://USER:PASSWORD@HOST:3306/SOURCE_DB" `
  --target-url "mysql+aiomysql://USER:PASSWORD@HOST:3306/TARGET_DB" `
  --dry-run `
  --report migration\reports\mysql_migration_dry_run.json
```

确认目标库、备份和停写窗口后再执行正式迁移。密码不要写入命令历史，建议使用受控环境变量或密钥管理系统。

源行的 `TenantId` 按租户 `Code` 解释，不能回退匹配另一租户主键。源资源跨表重复 ID、重复实体 ID、目标既存实体同 ID（即使同租户）或历史资源重编号都会在实体写入前拒绝；本工具不猜测完整引用映射，也不覆盖既有账户和身份。提交过的批次不能直接重跑来覆盖目标。需要合并或恢复迁移时，应另行建立明确的身份映射与审核流程，不得为绕过检查清空真实目标数据。

`--prepare-schema` 仅用于无表的隔离空库；正常目标先执行明确的数据库升级。预检不代替停写窗口和备份，其他进程不得并发修改目标。

## 文件迁移

安全发布要求 Python 平台支持 `dir_fd`、`O_NOFOLLOW`、`O_DIRECTORY`，以下为 Linux 示例。Windows 原生缺少这些能力时会在写入前拒绝，不提供跟随链接或覆盖目标的不安全降级。

```bash
python migration/scripts/migrate_files.py \
  --input migration/examples/file_migration_input.example.json \
  --target-root uploads \
  --report migration/reports/file_migration_report.csv
```

文件迁移会记录哈希和缺失项。迁移后必须抽样下载、检查权限，并确认租户边界。

`file_id`/`tenant_id` 必须是规范正整数，tenant_id为已映射的目标租户主键；已有普通文件、链接和符号链接均不覆盖。目标目录及祖先须由可信维护账号独占管理。全批不是文件事务，崩溃可能留下本次半成品，重跑将拒绝覆盖；应先人工核对哈希与报告再安排恢复。

随包示例只包含一个合成文本文件，`original_path` 相对仓库根目录解析，示例没有网络 URL。它用于展示输入字段，不是旧服务的数据清单。实际使用前，导出自己的可信清单并检查 `file_id`、`tenant_id` 和文件路径。

`--target-root` 会写入文件；本工具没有 `--dry-run` 参数。测试时选择独立目录，不要直接覆盖现有上传卷。输出 CSV 和哈希报告保存在不随源码发布的 `migration/reports/` 中。

## 向量迁移

Qdrant 到 PGVector 的迁移需要显式提供 collection、租户、知识库和目标连接串：

```powershell
python migration\scripts\qdrant_to_pgvector.py `
  --qdrant-url "http://QDRANT_HOST:6333" `
  --collection COLLECTION `
  --pg-url "postgresql://USER:PASSWORD@HOST:5432/eic_vector" `
  --tenant-id TENANT_ID `
  --knowledge-base-id KNOWLEDGE_BASE_ID
```

迁移后使用 `compare_retrieval.py` 对固定查询集做召回对比，不以“导入条数相同”替代质量验证。

此处 `--pg-url` 直接传给 `asyncpg.connect`，使用 `postgresql://`，不是应用 SQLAlchemy 设置中的 `postgresql+asyncpg://`。脚本会写入目标库，应先检查向量维度、租户与知识库 ID、备份和目标权限。

## 不应放入公开源码的内容

- `backend/.env` 中的密钥
- `data/`、`uploads/` 下的真实数据
- 运行日志、缓存、容器层和临时报告
- 生产环境的备份副本
- 未确认许可证的第三方素材

这些排除规则只针对开源源码包，不表示升级服务时可以丢弃真实数据库、上传文件或原加密密钥。旧服务向新服务迁移时，这些运行态必须在受控环境中单独备份、传输和验证，不得混入公开仓库。
