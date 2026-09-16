# project-continuation/v1 候选（未发布）

TS-083 同产品 CLI envelope 形状，供协调者审查。根 contracts 才是跨产品正式发布入口；本目录不是已冻结合同，不改既有 text-dialogue/profile-memory/source-sync，也不改已发布的 project-knowledge/v1 与 project-directory-import/v1 候选。

参考 `schema.json` 和 `examples.json`。两个只读 operation 与官方 SDK `tools/list` 输出的 `knowledge_continuation_recover` / `knowledge_continuation_check` 一一对应：

- `continuation_recover`：对一个**已登记**工作目录生成接续包，参数只有 `worktree`（登记 id）、`text`（检索词）与 `budget_bytes`（4096–32768）。包内三段严格分开：`history`（仓库历史，最多 8 条，超过则 `complete=false`）、`index`（本服务已索引的文档版本与哈希）、`worktree`（当前检出的分支/HEAD/脏状态计数/登记文件摘要）。只有 `worktree` 是本次实测；`index` 中未登记摘要的项标记 `unverified`，绝不把旧哈希当作当前文件。
- `continuation_check`：原样回传一个包，服务端重新观测该工作目录并给出裁决：`valid`、`reason`、按固定优先级排列的 `differences` 与 `observed`。切分支、新提交、新未提交改动、登记文件变化、索引/revision 变化、证据失效都使包不再有效；工作目录无法观测时返回 `observed=false` 的裁决而不是静默成功。

接续必须绑定显式登记的工作目录（`knowledge.worktrees.<project>`，字段 `id`/`path`/`branch`/`clients`/`files`/`max_bytes`）。两个编码体可以各自登记自己的检出：`clients` 决定谁能使用哪个工作目录，未登记或属于他人的 id 一律 `worktree_unregistered`，不泄露是否存在。采集器只做固定只读 Git 调用（`rev-parse`/`symbolic-ref`/`status`/`log`/`--version`，全部带 `--no-optional-locks` 与 `-c core.fsmonitor=false`），不 fetch/checkout/reset/clean、不读 diff、不读未跟踪文件正文、不列出脏文件路径（只给分类计数），且文件摘要只读操作者登记的少量路径（单项 1 MiB，登记总字节上限）。包不回声宿主绝对路径。

`recent_verification` 允许两种形状：纯字符串（永远标记 `scope=unbound`）或 `{summary, worktree, commit}`。结构化条目由服务端在写入时盖上实际观测到的 `commit`/`branch`/`dirty`/`declared_at`：调用方声明一个并非当前 HEAD 的 commit 会被 `workdir_conflict` 拒绝，因此历史测试结果不能冒充当前提交的测试。读取时按实测重新标注 `scope`（`current`/`historical_commit`/`workdir_changed`/`other_worktree`/`unbound`）。

字节预算只约束可选资料单元：必读部分（工作目录事实、历史、索引、已声明状态）从不切分，装不下时以 `budget.over_budget=true` 明示。没有 tokenizer，因此 `budget.tokenizer=null` 且 `token_counts=unavailable`，不伪报 token。

认证来自 stdio 进程配置 client + 独立凭据环境变量；权限 `continuation_recover` 与 `continuation_check` 与操作同名并分开授予，包的 seal 绑定 client 凭据摘要，另一个 client 无法校验他人的包。

双方待验：Hermes/Codex 真客户端如何展示历史、索引与当前未提交事实的差异；跨产品工作目录登记的生命周期（谁登记、谁撤销、迁移时如何迁移）；多检出并行协作时 `recent_verification` 的展示约定。当前不提供后台轮询、文件系统监听、跨产品 HTTP、共享数据库、模型调用，也不向任何外部模型发送代码。
