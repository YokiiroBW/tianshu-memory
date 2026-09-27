# CONNECT-M 记忆浏览接入交接

## 目标与范围

基于 Memory `9a3b2bed6aebff9f0677f2c62e979859769e0c9c`，提供给 Platform 服务端使用的真实人物/群共享画像与本人记忆分页入口。仅修改本 Memory 任务检出，不写根合同、其他产品、NAS、真实数据或凭据。本批用户授权已有能力整体贯通，接口先行稿为 [CONNECT-M-API.md](CONNECT-M-API.md)。

固定实现提交：浏览端点 `0dd542a76cb7db36013513b3de28b81c2c81ac05`；知识四项只读扩展 `bd0123bc7e242dc5a767347602f23957af9b2c33`。本交接收尾不改变这两个固定实现提交。

## 变更

- 新增三个只读 POST `/internal/v1/memory/browser/{overview,subjects,records}`。服务端固定 `browser_readers.<caller>` 账号、actor、精确 scopes；专用 `callers.<caller>` 只授 `browse`，按现有 `Authenticator` 逐请求读取 Bearer 与 Platform HTTPS origin。请求中的人、actor、scope 不能扩大固定权限。
- SourceAuthority 对 owner facts、当前 grant、final head 执行原有同步屏障并先提交来源负面状态。冻结 source-sync/v1 的 viewer 校验只支持 `authenticated_service=companion`，因此浏览链使用其无 viewer 屏障，不伪造 Companion；之后在**不持有数据库事务**时再次认证服务凭据、真实解析同一 Platform origin、重读固定映射，然后在本地事务内比 source_revision、binding 与完整 scope。这个组合方案仍待总控与 A 审查后发布；不宣称与冻结 viewer 联合快照合同等价。
- 目录只遍历当前授权 scope 中实际有效的 groups/records 和 profile_shares，检查 lineage 与 projection 当前性，返回完整语义组但剥离内部来源。HMAC 游标绑定视图、subject、来源 revision 和 scope version；来源变化返回 409。请求体 16 KiB、每页 50、最多扫描 200 候选、响应 256 KiB，沿用现有请求并发/两段超时/诊断/TLS/Host/Origin 拒绝。
- 无新增迁移和写操作。`identity/link` 依旧不可用，画像批准/修订确认仅沿用 `LocalUserApplication` 明确用户操作；服务 Bearer 不授予同意。

## 验证

隔离使用真实 Memory SQLite/SourceAuthority、合成 HTTPS Core/Platform owner、真实 Memory CLI `serve` HTTP 子进程。`tests/test_browser_catalog.py` 覆盖空态、当前记录与分享、来源 grant 拒绝、跨 actor/账号、凭据轮换、origin 过期、屏障期间 origin 撤销/缩 scope/凭据轮换、来源负面状态独立提交、分页/跨视图游标、预算边界与浏览 Origin 拒绝。

- `uv run ruff check src/tianshu_memory/app.py src/tianshu_memory/browser.py tests/test_browser_catalog.py`：通过。
- `uv run ruff format --check src/tianshu_memory/app.py src/tianshu_memory/browser.py tests/test_browser_catalog.py`：通过。
- `uv run pytest tests/test_browser_catalog.py tests/test_source_sync.py tests/test_profile_http.py -q --basetemp .runtime/tests-connect-m-targeted`：107 通过；测试 CA 工具解释器按 AGENTS 指向 bundled Python。
- `uv run pytest -q --basetemp .runtime/tests-connect-m-full`：988 通过、6 跳过、2 条已知依赖弃用警告（浏览响应预算/画像单元验证最后两处定向收尾后不重复全套）。所有证据仅为隔离夹具；未连接真实 Platform 用户或 NAS。

## 接入与未完成

Platform 必须通过正式 Origins.issue 为已登录账号/actor/scope 发放 `platform→memory dialogue` origin；Memory issuer 的独立 resolver 凭据配置 `caller=platform,purpose=dialogue`，并通过正式 route 授权。配置示例和 wire 见 API 文档；总控发放私有配置，不把 token/origin 暴露给浏览器。B 服务器可消费上述三端点，U 只连 B 的会话后端。A 对无 viewer 组合验证和跨范围投影单独验收后，再由总控集成和 NAS 真实数据验证。

## 第二提交：项目经验与交接知识 HTTP

用户整体贯通目标新增本 Memory 精确范围。既有 `KnowledgeApplication.execute` 的 `lesson_query`、`experience_query`、`continuation_recover`、`continuation_check` 四个读操作接入**原** `/local/v1/project-knowledge/action`。旧十二操作兼容；新四项对每个固定 knowledge client 默认关闭，必须另外配置 `http_read_operations` 显式列入，再经过领域已有同名 permission/项目授权；经验查询仍独立要求 `review`。入口动作前和领域完成后均重读新增 HTTP 授权，撤权结果不交付。不开放记录、修订、经验晋升、目录扫描/应用或任意 Git 命令。详见 [CONNECT-M-KNOWLEDGE-API.md](CONNECT-M-KNOWLEDGE-API.md)。

`continuation_recover/check` 实际只读已登记 checkout 的 Git/文件和现有项目数据库，生成/验证客户端绑定 seal；不会向 `knowledge_operations` 持久化封装包、不会修改 checkout。包是当次观测快照，不是持续最新状态；必须在网页明示“读取交接”动作时触发，不能后台轮询。受信 Platform 后端只投影安全字段，不把完整包、seal 或本机路径显示给浏览器。`lesson_query`/`experience_query` 为文本检索，不是全量目录；真实没有匹配时按空态展示。

验证：`uv run pytest tests/test_knowledge_http_extra.py tests/test_knowledge_http.py tests/test_knowledge_http_process.py tests/test_knowledge_catalog_http.py tests/test_lessons.py tests/test_knowledge_continuation.py -q --basetemp .runtime/tests-connect-m-knowledge-suite` 为 179 通过、1 跳过；最后把执行中撤销 HTTP 授权的拒绝状态固定为传输层 415 后，`test_knowledge_http_extra.py` 4 项复核通过。用真实独立 HTTP 子进程读到非空 lesson/experience/continuation，lesson 与既有 CLI 实际输出相同；小预算、跨项目 review、未登记 checkout、旧客户端默认拒绝和授权中途撤销均验。`ruff check`/`ruff format --check` 与完整 diff 检查通过。部署、真实发放、完整浏览器链与用户写动作未在本任务执行。
