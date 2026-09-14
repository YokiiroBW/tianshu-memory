# 题目候选：项目错题本与总经验 v2（TS-081）

本目录是**本产品内部候选**协议，不是已发布根合同；不冻结、不代表 Hermes/Codex 已接入。v2 在 `docs/candidates/project-knowledge/v1` 的操作集合上追加错题本与总经验操作，v1 语义不变，因此 v2 完全向后兼容 v1 的操作与参数。

## 与 v1 的差异

新增 10 个操作，共用同一 `{operation, project_id, arguments}` 信封：

| 操作 | 权限 | 作用 |
| --- | --- | --- |
| `lesson_record` | 项目 `lesson_record` | 录入一条有版本的项目错题，必须带当前来源块证据 |
| `lesson_revise` | 项目 `lesson_revise` | 追加新版本，旧版本留在历史 |
| `lesson_retire` | 项目 `lesson_retire` | 废弃该错题由召回；引用它的总经验立即不可复用 |
| `lesson_query` | 项目 `lesson_query` | 本项目错题定向检索，预算内整条返回 |
| `lesson_recover` | 项目 `lesson_recover` | 短恢复：目标、未完成项、当前错题与引用 |
| `lesson_check` | 项目 `lesson_check` | 复用本地恢复包前检查当前性 |
| `experience_promote` | 客户端 `promote` | 显式批准一条全局总经验，至少两个不同项目的有效证据 |
| `experience_query` | 客户端 `query` + `review` | 检索已批准总经验，只返回调用者有权来源的条目 |
| `experience_check` | 客户端 `review` | 复用总经验前检查；来源撤销/修订/废弃即失效 |
| `experience_revoke` | 客户端 `review` | 撤销一次全局晋升 |
| `experience_withdraw` | 客户端 `review` + 项目 | 来源项目把自己的错题从某条总经验中撤出 |

`experience_promote` 不新增 `project_id` 语义：调用者仍需对信封中的 `project_id` 有相应项目权限，且 `entry.evidence` 中出现的每个 `project_id` 都必须同时在该客户端的 `projects` 与 `permissions` 中，否则在读取任何证据前失败关闭。

## 幂等键：`key` 与 `dedupe`

错题的 `key` 同时是它的**稳定身份**（`lesson_id` 由其派生），因此不能为"再写一次"换键。可选 `dedupe` 分离两件事：

- 省略 `dedupe`：本次请求的幂等键就是 `key`。同一身份只能成功写入一次，之后同负载返回历史结果，异负载 `idempotency_conflict`。
- 提供 `dedupe`：`key` 仍表示身份，`dedupe` 表示本次请求。`lesson_revise` 用它把同一错题修订到新版本；`experience_promote` 用它再次批准同一条目（复核/恢复）；`experience_revoke` 与 `experience_withdraw` 用它表达不同的处置动作。
- `lesson_revise` 的 `key` 必须与记录时一致（否则 `identity_conflict`）；`experience_promote` 的 `expected_version` 为当前版本时，`entry.evidence` 必须与已验证集合完全一致（否则 `evidence_changed`）。

## 不变量

1. 错题证据是 `query` 返回的当前块引用（`block_id`/`document_id`/`version`/`hash`），必须属于信封中的项目；跨项目块引用即使调用者能读两个项目也被拒绝。
2. 错题版本的内容指纹绑定其来源块的**内容哈希**：重复导入相同字节不改变指纹，来源被修改或删除立即改变。读取时重新核对，过期条目不返回并计入 `omissions`。
3. 晋升需要 `promote` 独立权限；同一项目的重复日志不满足“两个不同项目”。模型判断、聊天回执、自动日志抓取都不构成晋升依据。
4. 全局条目只保存操作者显式确认可共享的摘要（`title`/`rule`/`applicability`/`excludes`/`counterexamples`/`recheck_after`），项目私密正文与文件路径不复制到全局；默认只返回受保护的引用，调用者显式按项目过滤时才返回本有权项目的引用。
5. 总经验的有效性在读取时推导：来源错题被修订/废弃、来源被删除或修改、来源项目被撤权、来源项目主动撤出，都会让条目变为 `superseded`/`stale_source`/`withdrawn`/`unavailable`，不再返回且 `experience_check` 判定失效。
6. 只返回调用者授权的内容：来源项目集合不是调用者授权集合子集时，条目既不出现在检索结果，也不在 `check` 中泄露存在性。
7. 写入操作沿用幂等键：同负载重放返回历史结果，异负载 `idempotency_conflict`；版本竞争使用 `expected_version`，冲突返回 `version_conflict`。错题身份的 `key` 与请求的 `dedupe` 分开（见上一节）。
8. 每个操作分三段执行：事务内只做数据捕获（无文件/网络 I/O）、事务外读取证据、短事务复核并提交。因此慢来源不占用共享写锁，文件在两段之间变化一律判为过期。

## 版本说明

- `urn:tianshu:candidate:project-lessons:2`。`$id` 与目录名绑定；v1 目录保持不变。
- 与 v1 相同：`arguments` 是纯数据，不声明身份；客户端身份只来自本地启动参数与独立凭据。
- 已知未覆盖：受信任的跨产品评审签名、总经验的多语言检索质量、过期时间到达后的自动失效（当前 `recheck_after` 仅作为操作者声明的复核提示返回，不自动改变可用性）。

## 示例

`examples.json` 与 `schema.json` 由 `tests/test_lessons.py::test_candidate_schema_and_examples_cover_new_operations` 校验。
