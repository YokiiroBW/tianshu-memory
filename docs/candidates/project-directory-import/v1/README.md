# project-directory-import/v1 候选（未发布）

TS-082 同产品 CLI envelope 形状，供协调者审查。根 contracts 才是跨产品正式发布入口；本目录不是已冻结合同，不改既有 text-dialogue/profile-memory/source-sync，也不改已发布的 project-knowledge/v1 候选。

参考 `schema.json` 和 `examples.json`。两个 operation 与官方 SDK `tools/list` 输出的 `knowledge_directory_scan` / `knowledge_directory_apply` 一一对应：

- `directory_scan`：只读预览一个**已登记**目录，参数只有 `directory`。返回计划（`plan_id`、逐项 locator/类型/大小/摘要/当前索引版本、`omitted`、`missing`、`counts`、`walk_truncated`、`budget_incomplete`、`missing_truncated`、`deletions=explicit_approval_only`、`scan=registered_directory_only`）。不写项目数据、不创建项目行、不监听文件系统。
- `directory_apply`：确认一个**本服务发给同一 client/项目/目录**的预览，参数为 `key`、原样回传的 `plan`、显式批准的 `tombstones`。逐项复核路径/类型/大小/摘要/当前版本，变化即该项 conflict；结果持久记录，可用 `status` 按 key 读回。

幂等键在读取任何来源之前绑定：同一 client 的同一 key 只接受逐字节相同的请求（操作、项目、计划、tombstones 都参与摘要），同键异项目/异计划/异操作/异负载在任何写入前就返回 `idempotency_conflict`，不会先写再报冲突；完全相同的请求可续做被中断的进度或返回先完成者的 `replayed` 结果；未完成的绑定在 `status` 中显示为 `in_progress`，不是成功也不是历史结果。

认证来自 stdio 进程配置 client + 独立凭据环境变量，project_id 仅选择已授权范围；目录登记（`knowledge.directories`）决定可扫描的目录与文件数/字节上限，调用方不能自行放宽。权限 `directory_scan` 与 `directory_apply` 分开授予。计划指纹只提供一致性与防篡改，不是授权令牌。

双方待验：Hermes 真客户端的预览展示与确认交互、大目录预览的呈现预算、跨产品登记目录生命周期（谁登记、谁撤销、迁移时如何迁移）。当前不提供目录自动监控、后台增量同步、跨产品 HTTP、共享数据库、模型整理或删除真实文件。