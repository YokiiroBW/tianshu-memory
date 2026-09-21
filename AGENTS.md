# 天枢记忆开发约定

TS-080 项目知识域通过独立 `KnowledgeApplication` + Store 事务处理，不伪造聊天 SourceAuthority。知识表迁移只能在 schema 3 显式备份后执行，所有权威表继续受 source-guard 跟踪。配置与命令见 `docs/project-knowledge.md`；专项 `uv run --extra mcp pytest tests/test_knowledge.py tests/test_knowledge_transport.py tests/test_knowledge_concurrency.py -q --basetemp .runtime/tests-ts080-targeted`，跨迁移完整验证 `uv run --extra mcp pytest -q --basetemp .runtime/tests-ts080`。MCP 是官方可选 extra，不默认启用、不自动登记客户端。

先读主工作区 `docs/development/CURRENT.md`、当前任务卡和已发布合同。本仓库任务检出上下文见 `.runtime/workspace-context.json`。只改已分配范围；`projects/` 协调检出、根任务板与共享 schema 由协调者维护。

采用小模块 Python 后端。`src/tianshu_memory/` 内身份、范围、记录和索引由本服务拥有；外部来源核验通过显式内部端口。不得把模型提议、短期来源字符串或服务令牌本身视为用户确认。来源缺失明确不可用；不加假成功或假向量。

TS-033 正式来源入口为 `SourceAuthority` + `SourceTransport`，绑定 source-sync/v1 manifest `178d0ce66210bdfad4cfb85d8b5f0905b0b67f834e2a530efe5636ff0373633d`。所有读取/消费/候选提交/修订/check 必须走 `MemoryService.operation`；远端同步失效事务提交后，再比较本地 revision 开业务事务。身份 resolve/register 不依赖远端来源查询。生产服务不得继承 fixture 后端或借 LocalWorkflow 写入。

## 真实命令

环境安装：`uv sync --locked --group dev`。

语法/静态：`uv run ruff check .`；`uv run ruff format --check .`；`uv run python -m compileall -q src tests scripts`。

受影响组件：`uv run pytest tests/test_identity_auth.py tests/test_recall.py tests/test_recall_regressions.py tests/test_revisions_events.py -q`。

来源接线：`uv run pytest tests/test_source_transport.py tests/test_source_migration.py tests/test_trusted_workflow.py tests/test_source_sync.py -q`。迁移/事务/认证共享变动后再跑完整产品测试。

本地用户入口：`uv run pytest tests/test_user_actions.py tests/test_trusted_workflow.py -q --basetemp .runtime/tests-ts034-users`。HTTPS 测试前设置 `TIANSHU_TEST_CERT_PYTHON` 指向已有 cryptography 的工具解释器，见 `docs/runtime.md`；完整测试也使用 `.runtime/` 下独立 basetemp。

TS-085 受限 HTTP 入口：`uv run pytest tests/test_knowledge_http.py tests/test_knowledge_http_process.py -q --basetemp .runtime/tests-ts085-http`。前者用进程内 ASGI 客户端（含手写 ASGI 驱动，可统计应用索取正文的次数）覆盖身份/媒体类型/Host-Origin/准入先于读体/四慢体与四慢执行饱和/读体与执行两段超时/断连/调用抛错/领域回归，后者真实启动、停止、重启 `uv run python -m tianshu_memory.knowledge_cli --config <私有配置> serve --client <身份> --port <端口>` 子进程并经真实 socket 核对（子进程环境不含任何知识凭据）。端口无默认值、只绑 `127.0.0.1`，身份只能来自启动参数，凭据只来自每请求 `Authorization: Bearer`。它是内部受限接口候选，不是已发布跨产品合同，也不代表任何网页已联通；见 `docs/project-knowledge-http.md`。

TS-086 项目资料目录与分页阅读：`uv run pytest tests/test_knowledge_catalog.py tests/test_knowledge_catalog_http.py -q --basetemp .runtime/tests-ts086-catalog` 与 `uv run pytest tests/test_knowledge_catalog_migration.py -q --basetemp .runtime/tests-ts086-migration`。新增只读 `document_list`/`document_read`（各需同名显式 permission，`query` 不隐含），并把受限 HTTP allowlist 由十项增至十二项；迁移是停写后的显式步骤 `uv run python -m tianshu_memory.knowledge_cli --config <私有配置> migrate-catalog --backup <新文件>`，独占备份 + 既有 `Store.transaction()` + schema 3 source-guard 检查点，`store.py`/`source_recovery.py` 零改动，旧备份不可直接盖回；见 `docs/project-knowledge-catalog.md`。

完整本产品：`uv run pytest -q`，包含独立本地 HTTP 子进程启动、停止、重启验证。来源/身份 issuer 使用明确测试替身，不声称跨产品 L0 或真实外部验收。

隔离启动和合同环境变量唯一说明见 README。不要自行发明其他检查层级；完整改动稳定后审查 diff，再从低成本到高成本运行所需检查，成功且输入未变的检查不重复运行。

## 交付边界

本地数据库、凭据和运行配置只放被忽略目录；不入库真实原文或数据。SQLite/WAL 是本地首切片，不能把它的验证算作生产 PostgreSQL 通过。生产来源/确认/账号关联与嵌入合同接入独立记录。

schema 3 使用独立 `source-guard.json` 检查点防止旧数据库恢复丢失 suppression/消费账本/owner 水位；所有写入必须用 Store 事务。缺失/不一致不可重建为 ready，不覆盖检查点来“修复”测试或恢复；恢复批准/重建另立任务。迁移、备份、失败关闭与停止写入要求见 `docs/source-sync-runtime.md`。

任务交付 `docs/handoffs/<任务编号>.md`，附实际测试、未完成、合同和本地提交；协调者审查后才能标完成。不自动合并、推送、部署或触发下游任务。

TS-034 本地用户应用只接受完整显式操作；独立部署 token 摘要映射稳定账号/actor/精确权限，实时 Platform resolve 与 Memory binding 验证。禁止将任意 True 适配器、服务 token 或来源受理视为批准。画像扩展通过 `migrate-users --backup` 显式迁移，新增表必须受 source-guard 跟踪；配置与回滚边界见 `docs/local-user-actions.md`。
