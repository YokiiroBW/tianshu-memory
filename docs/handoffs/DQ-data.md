# DQ11 / DQ13：来源完整覆盖与文件新鲜度复用

本轮基线 `1f3121c9758faeb31fc9d0fe2a54974c72505d55`，分支 `codex/quality-life-20261003`。状态为本地开发与隔离验证，未集成、推送、部署或迁移生产库；未读取真实聊天、凭据或 NAS。根合同及任务板由协调者维护。

## 实现

- DQ11：`SourceAuthority` 保留完整 text 历史来源和原 profile 覆盖规则，按原协议每批最多 256 selector 读取。一个完整屏障的全部 Core generation/sequence、全部 Platform generation/sequence 和 viewer 完全一致；多批结束后追加空 admissions 的 Platform current 与 Core head 复核。变动或失败丢弃整轮，不提交前缀；本地 m0/coverage 改变也整轮重试。聚合后一次调用既有 `_apply`，同一事务应用所有失效、版本和 owner 水位，不存跳过历史的游标。
- 绑定协调者发布的独立 `source-sync-batch/v1` 1.0.0，固定 LF manifest SHA256 `82e8d9043eb2f9a87f0efcb56a16e8f8bb6b119c6cf1683af48e3049024b380b`。启动按现有 `Contracts` 校验全部包文件和固定原 source/text/profile 依赖；原 `source-sync/v1` hash/wire 不变，Core/Platform 无需新增生产接口，也没有用户开关。
- 回执身份检查复用数据库查询，新增非唯一 JSON 表达式索引 `admissions_receipt`；历史空 payload/receipt 不被清理或加唯一约束。另建 `sources_scope` 支持 scope 覆盖。旧 schema 3 需停写后通过 `migrate-source-lookup --backup <新文件>` 显式迁移，独占新备份、验证原 source-guard、保持权威 payload、同步 revision/checkpoint。新 `migrate-sources` 在同一既有迁移中安装该能力；缺失迁移失败关闭，不自动修库。命令和回退边界见 [来源运行接线](../source-sync-runtime.md)。
- 修复同水位 Platform grant 比较把 `admission_digest` 当作授权事实的问题：它是 Core 输入关联，每批仍由 `current_access` 验证；仅比较时排除这个关联字段，其他实际授权字段同水位变化仍拒绝。晚到 Core 修订由此能保留原 Platform 授权语义。
- DQ13：相同的 capture/read/serve 内容哈希、双读验证、过期拒绝和短期缓存实现归于已有 `knowledge_sources.FileReader`；知识 `CatalogReader` 与 `LessonReader` 复用，后者仅保留自己的 `hash_of` 投影。并发夹具改为在实际规则归属处注入变更。

## 验证边界

使用仓库声明并锁定的 Python 3.12.14 / dev / MCP 依赖，HTTPS 为 loopback 合成 Core/Platform/issuer，SQLite、备份、guard 和进程配置全部独立放于 ignored `.runtime/`。

新增用例覆盖 257/513 来源完整覆盖与真实 Memory 子进程重启、513 来源中晚到修订/撤回、跨批 owner/viewer/缺失 admission/null source/重复 receipt 拒绝、并发本地 revision 整轮重试，以及 301 记录失效仅推进同域一次。迁移验证权威表与旧空 payload 保留、备份完整、guard 更新、拒绝已有备份/数据库/guard 目标、丢失 guard 和注入中断回滚。合同验证扩展及其全部依赖的字节漂移拒绝。没有扩大未测量的 recall 优化。

两份联合样例各有 **257 selector / 2 batch**，不是 513；由消费者实际屏障捕获并交协调者独立通过原 `sync_barrier` 与新 schema/rules 验证。513 来源计时是另一隔离用例：定向验证 self_private 2.535 秒 / group 2.585 秒；完整回归时分别 3.128 / 2.812 秒，同步应用事务内观测约 0.109 / 0.078 秒（不包含进入事务前的锁等待）。每轮 513 次 receipt 查询，SQLite EXPLAIN 使用 `admissions_receipt`，无逐 receipt 全表 JSON 扫描。计时不代表 NAS 或真实端到端性能。

生产已有 Memory 执行预算 15 秒、SourceTransport 每次 owner 请求 5 秒，Companion query/command 总预算 15 秒、Platform WebMemory/WebReader 10 秒。没有修改这些预算；513 重启用例测试客户端单次设为 20 秒只是消除既有夹具默认 3 秒的测试限制。完整历史更大时仍受既有请求/响应字节及执行时限约束，不承诺无界性能。

## 实际验证结果

- `uv sync --locked --extra mcp --group dev --python <已有工具 Python>` 成功；使用既有 pytest/ruff，没有新增测试框架或生产依赖。
- 稳定代码先运行文件新鲜度/lessons/来源迁移专项，101 passed；再修复新增晚到修订回归中暴露的 input digest 比较问题，相关修订/撤回/真实授权变化/大批失效/合同样例/索引验证 12 passed。
- 完整命令 `uv run --extra mcp pytest -q --tb=short --basetemp .runtime/tests-dq-data-full --junitxml .runtime/dq-data-full.xml -o junit_family=xunit1`：1133 项，首次 **1090 passed / 4 failed / 33 errors / 6 skipped**，504.72 秒。33 个 error、3 个 fail 和 6 个 skip 均因既有诊断/serve 夹具读取 ignored `.runtime/workspace-context.json` 而不采用合同环境变量；补仅指向本轮隔离 coordination 的指针后，只补跑这些失败和未执行用例：36 项加修正后的大批失效夹具 2 项 **38 passed**（11.94 秒），原 6 skip **6 passed**（0.11 秒）。结果分别保存 `.runtime/dq-data-repair.xml`、`.runtime/dq-data-skips.xml`，不重复整套已通过检查。
- 合并完整覆盖的有效结果为 **1132 passed / 1 既有 failed / 0 skipped**。剩下 `tests/test_trusted_workflow.py::test_confirmation_rejects_mismatched_authority[account-403]` 使用 QQ `unknown-account` 字符串，先触发格式校验 400，而断言期望 403。用 `git archive` 导出精确基线 `1f3121c9758faeb31fc9d0fe2a54974c72505d55`、同一锁定依赖和合同独立执行该单例，复现同样 400 != 403（`.runtime/dq-baseline.xml`）。本轮未修改该测试、workflow 或授权规则，不扩展任务修它；没有新增业务失败，但不能称完整套件全绿。
- `uv run ruff check .`、`uv run python -m compileall -q src tests scripts`、`git diff --check` 通过；本轮修改的 Python 文件 `ruff format --check` 通过。整库 `ruff format --check .` 仍要求格式化基线未修改的 `docs/relationships.md`、`src/tianshu_memory/auth.py`、`src/tianshu_memory/role_grants.py` 和 `tests/test_role_grants.py`，保留原内容，没有顺手重格式化。
- `uv run tianshu-memory --help` 可见新增显式迁移命令；未给真实配置执行迁移。完整回归包括既有独立 HTTP/MCP/CLI/启动停止重启、并发、知识目录、关系和授权用例；TLS issuer/Core/Platform 仍是替身，跨产品及真实服务验收不包含在此结果中。

本地提交包含此交接与上述范围；准确 SHA 由提交完成后的协调回报提供。下一步由协调者审阅并串行集成共享扩展与消费者；实际部署旧 schema 3 时另安排显式备份迁移。
