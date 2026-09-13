# 天枢记忆开发约定

先读主工作区 `docs/development/CURRENT.md`、当前任务卡和已发布合同。本仓库任务检出上下文见 `.runtime/workspace-context.json`。只改已分配范围；`projects/` 协调检出、根任务板与共享 schema 由协调者维护。

采用小模块 Python 后端。`src/tianshu_memory/` 内身份、范围、记录和索引由本服务拥有；外部来源核验通过显式内部端口。不得把模型提议、短期来源字符串或服务令牌本身视为用户确认。来源缺失明确不可用；不加假成功或假向量。

## 真实命令

环境安装：`uv sync --locked --group dev`。

语法/静态：`uv run ruff check .`；`uv run ruff format --check .`；`uv run python -m compileall -q src tests scripts`。

受影响组件：`uv run pytest tests/test_identity_auth.py tests/test_recall.py tests/test_revisions_events.py -q`。

完整本产品：`uv run pytest -q`，包含独立本地 HTTP 子进程启动、停止、重启验证。来源/身份 issuer 使用明确测试替身，不声称跨产品 L0 或真实外部验收。

隔离启动和合同环境变量唯一说明见 README。不要自行发明其他检查层级；完整改动稳定后审查 diff，再从低成本到高成本运行所需检查，成功且输入未变的检查不重复运行。

## 交付边界

本地数据库、凭据和运行配置只放被忽略目录；不入库真实原文或数据。SQLite/WAL 是本地首切片，不能把它的验证算作生产 PostgreSQL 通过。生产来源/确认/账号关联与嵌入合同接入独立记录。

任务交付 `docs/handoffs/<任务编号>.md`，附实际测试、未完成、合同和本地提交；协调者审查后才能标完成。不自动合并、推送、部署或触发下游任务。
