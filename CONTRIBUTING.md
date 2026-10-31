# 贡献指南

## 开始前

请先阅读 `README.md`、`docs/ARCHITECTURE.md` 和 `SECURITY.md`。提交前确认没有加入数据库、上传文件、`.env`、凭据、日志或本机路径。

## 开发流程

先按[安装说明](docs/GETTING_STARTED.md)准备项目专用虚拟环境。以下检查供贡献者自行执行，不使用系统 Python 安装项目依赖：

```powershell
backend\.venv\Scripts\python.exe -m pip install -e ".\backend[dev]"
cd frontend
npm ci
npm run check
npm run build
cd ..
backend\.venv\Scripts\python.exe scripts\test-isolated.py --output "$env:TEMP\eic-tests"
```

后端回归必须使用 Python 3.11，并通过 `scripts/test-isolated.py` 逐模块创建临时源码、数据库和上传目录。不要直接在仓库根目录或系统 Python 中运行全量 `pytest -q`，否则相对数据库配置和 Windows 编码环境可能污染工作区或产生误判。

后端代码遵循现有 `ruff` 配置；前端保持 Vue 3 和现有 UI 组件约定。新增用户可见错误信息时，优先使用 `frontend/src/utils/userMessages.js` 的中文映射。

`npm run check` 执行源码语法审查和 `vue-tsc --noEmit`；`npm run typecheck` 可单独执行。类型检查沿用现有非严格配置，未开启全部 JavaScript 的 `checkJs`，不能替代交互验证。`npm run build` 随后检查产物静态依赖、循环与分块体积。

前端保留 DOM fixture 和测试源码；不要把构建成功或 `tests 0` 写成测试通过，源码级检查也不能替代浏览器交互验证。提交前请记录本次实际执行的命令、结果和未覆盖范围。

## Pull Request 要求

- 说明问题、方案和影响范围。
- 附上测试命令和结果。
- 涉及数据库时提供迁移和回滚说明。
- 涉及安全边界时增加负向测试。
- 不提交真实模型密钥、生产 URL、用户资料或内部报告。
