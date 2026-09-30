# MEMORY-ROLE-20260930 · Memory 交接

基线：已部署 Memory `870d9269d33b88ed34c6445eb7d95f791b43a914`。候选实现提交：`da14cfd91675b2a0d400227b916e3f3a299904b6`；本交接文件为后续文档提交。仅在本产品独立 worktree 开发，未修改权威 DB/schema、根仓库、`projects/` 或 NAS。

## 变更

`browser.py` 保留静态 `browser_readers.<caller>.account`、`actor_id`、完整 `scopes` 精确匹配。动态角色需同时满足 reader `allow_runtime_roles: true`、caller `allow_runtime_roles: true`、持久 role grant 对该 actor 当前精确 enabled，以及原模板的 person/audience/conversation 完全相同；每次读取和来源屏障后的二次授权都重新校验。静态 actor 一旦被 grant 接管且停用，既有 `Authenticator.resolve` 仍拒绝。没有新增记忆事实或第二套查询表，概览、人物/群、记录与游标沿用完整 scope 过滤。

部署配置只需在现有 `callers.platform` 与 `browser_readers.platform` 各增 `"allow_runtime_roles": true`，并保留 `role_grants_database_path`、原静态 `allowed_actors`、`operations: ["browse"]`、reader 的 `account`、`actor_id` 和完整 `scopes`。不使用通配 actor，不更改来源 issuer 或实际记忆数据库。两门控任一缺失即拒绝动态角色，原静态配置不变时查询行为保持。

## 已完成验证

- `test_browser_catalog.py` 12 项通过。真实 Memory ASGI + HTTPS owner 夹具以同 person/conversation 双角色区分语义组和概览，拒绝伪造来源、错误会话模板、缺任一动态门控、撤销 grant、停用 `browse`；旧静态读取保持。
- Platform 候选 `test_memory_joint.py` 通过真实 HTTPS Platform issuer → Memory TLS 进程联验，含双角色人物投影、分页/跨角色游标、静态角色停用与 Memory 进程重启；命令与截图见 Platform 同名交接。
- 最窄命令：`TIANSHU_CONTRACT_DIRECTORY=<根 contracts/text-dialogue/v1> TIANSHU_TEST_CERT_PYTHON=<证书工具解释器> PYTHONPATH=<本产品 src> python -m pytest tests/test_browser_catalog.py -q --basetemp .runtime/tests-memory-role-browser`。复用已存在的隔离 Python 依赖环境；没有改他人检出。
- 全量 Memory 测试：补齐本 worktree 忽略目录 `.runtime/workspace-context.json` 指向协调仓库后，`python -m pytest -q --basetemp .runtime/tests-memory-role-full-2` 得到 `1001 passed, 6 skipped`（253.43 秒）。首轮缺这个上下文而失败，不属于产品回归。

## 边界

当前 Platform 网页模板是 `self_private`；已发布 profile 合同规定 group subject 使用 `group_only` 且需 group audience。角色选择不会把当前账号的私聊来源扩大成群范围。真实 NAS 配置/部署/实机验收由总控独立完成。
