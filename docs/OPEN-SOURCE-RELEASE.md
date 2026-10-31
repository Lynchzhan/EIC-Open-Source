# 开源发布说明

## 发布范围

`EIC-Open-Source` 是可独立发布的 EIC 企智枢源码工作区，保留后端、前端、数据库迁移、部署示例、Harness 评测和文档。它不包含用户数据库、上传文件、生产备份、模型密钥、云端地址或运行日志。公开发布前仍应对准备上传的 Git diff 和归档内容做一次人工复核。

项目使用根目录 `LICENSE` 中的 Mulan PSL v2。`NOTICE.md` 记录项目署名、第三方依赖和 PyMuPDF/MuPDF 的 AGPL 或商业许可提醒；发布者应按实际分发方式确认 PyMuPDF 许可方案。

## 验证要求

每个公开候选版本都必须由 `.github/workflows/ci.yml` 在目标提交上重新验证。历史验证记录曾通过 139 个后端测试模块、前端 `npm ci` / `npm run check` / `npm run build` 和部署结构校验；历史结果不代替本次候选版本的 CI 结果。

隔离回归使用合成数据库、协议桩和离线网络保护，不等于真实模型质量、真实 MySQL/PGVector/Redis 容量、真实 Langfuse 远端可见性或生产浏览器验收。后端必须通过 `scripts/test-isolated.py` 运行，不要直接执行全量 `pytest -q`。

## 可选接入

MinerU 适配默认关闭，源码包不安装或启动 MinerU 引擎；部署者应根据 `docs/mineru-integration.md` 自行提供受控的私有服务和资源限制。

自建 Langfuse 适配默认关闭，按 `docs/LANGFUSE-LOCAL.md` 配置自己的实例、项目密钥和网络出口。密钥只能注入后端与 worker 的受控运行环境，不能放进前端变量、命令行或镜像层。

## 源码首发检查表

首发范围为 GitHub 源码仓库；暂不发布预构建镜像或在线演示服务。GitHub 按 tag 生成的源码归档属于该源码发布范围。

公开前逐项完成：

1. 确认 GitHub 所属账号或组织、仓库名、默认分支 `main` 和首发版本 `v0.1.0`；当前本地副本没有提交和远端，不能从它推断目标仓库。
2. 保留根目录 `LICENSE`、`NOTICE.md`、`SECURITY.md` 和本说明；按实际分发方式复核 PyMuPDF/MuPDF 的 AGPL 或商业许可，以及项目代码和素材的权利归属。
3. 在目标仓库启用 GitHub Private Vulnerability Reporting，并确认 Security 页面提供 **Report a vulnerability** 私密入口。`NOTICE.md` 邮箱用于一般项目联系，不作为安全报告入口。
4. 在 `main` 和 Pull Request 上运行 CI，确认后端隔离测试、部署结构校验、源码包成员策略与高置信度秘密扫描、前端检查和构建全部通过。
5. 逐项审查首次暂存区：运行 `git diff --cached --check`、`git diff --cached --stat` 和完整 `git diff --cached`；确认没有 `.env`、数据库、上传目录、备份、日志、私钥、内部报告或本机数据。不要只依赖 `.gitignore` 或自动秘密扫描。
6. 在干净克隆中按 README 完成本地模拟启动，确认双语说明、截图和文档链接正确；将实际执行的命令、结果和未覆盖范围写入发布记录。
7. 推送 `main` 后检查 GitHub 页面展示，再创建 `v0.1.0` Release。公开提交后不能保证从他人克隆或缓存中彻底撤回，因此首个 push 前必须完成上述审查。

后续如需发布源码以外的制品，再单独评估归档清单、SHA256 清单、镜像来源与 digest、部署密钥注入、数据保留、成本限制和回滚流程。模型服务条款、MinerU 部署许可和 Langfuse 数据保留策略也应在启用相应集成前单独复核。

本说明描述的是源码包的准备状态，不代表任何部署者已经完成真实模型、外部服务或生产流量验收。
