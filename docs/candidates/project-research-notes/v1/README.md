# 题目候选：项目研究笔记 v1（TS-084）

本目录是**本产品内部候选**协议，不是已发布根合同；不冻结、不代表 Hermes/Codex 已接入。v1 在 `docs/candidates/project-lessons/v2` 的操作集合上追加研究笔记操作，既有语义不变，因此完全向后兼容。

## 与 v2 的差异

新增 7 个操作，共用同一 `{operation, project_id, arguments}` 信封：

| 操作 | 权限 | 作用 |
| --- | --- | --- |
| `note_record` | 项目 `note_record` | 录入一条有版本的研究笔记：问题、来源论述、研究推论、未决问题、可选项目决策 |
| `note_revise` | 项目 `note_revise` | 追加新版本，旧版本与其引用留在历史 |
| `note_withdraw` | 项目 `note_withdraw` | 记录撤回，笔记离开检索索引，引用它的决定不再可复现 |
| `note_query` | 项目 `note_query` | 本项目笔记定向检索，预算内整条返回（含全部引用） |
| `note_recover` | 项目 `note_recover` | 短恢复包：命中笔记、目标、未完成项、`revision`/`registration`/`seal` |
| `note_status` | 项目 `note_status` | 读取某一历史版本及其引用的当前状态 |
| `note_check` | 项目 `note_check` | 复用本地恢复包前检查当前性 |

`note_recover` 不新增 `project_id` 语义：仍需对信封中的 `project_id` 有相应项目权限。`note_recover` **不**包含 `worktree`/`branch`/`head` 等字段——项目工作目录事实仍由接续包拥有并优先于任何笔记叙述。

## 幂等键：`key` 与 `dedupe`

笔记的 `key` 同时是它的**稳定身份**（`note_id = "note:" + fingerprint([project_id, key])`），因此不能为"再写一次"换键。可选 `dedupe` 分离两件事：

- 省略 `dedupe`：本次请求的幂等键就是 `key`。
- 提供 `dedupe`：`key` 仍表示身份，`dedupe` 表示本次请求。`note_revise` 用它把同一笔记修订到新版本；`note_withdraw` 用它表达不同的撤回动作（同一 `key` 只能撤回一次）。
- `note_revise`/`note_withdraw` 的 `key` 必须与记录时一致，否则 `identity_conflict`；`expected_version` 必须等于当前版本，否则 `version_conflict`。
- `note_record` 的身份只创建一次：重复录入是 `version_conflict`，不是第二个版本。同负载重放返回历史结果，异负载 `idempotency_conflict`。

## 不变量

1. 六类内容分开存放，不合并、不互相冒充：`question`（研究问题）、`source_statements`（来源论述，每条带操作者自己的话与实际读到的来源引用）、`inferences`（研究推论）、`open_questions`（未决问题）、`decision.summary`（项目决策）、`decision.basis`（决策依据）。
2. 来源引用是与本项目 `query` 当前返回一致的块引用（`block_id`/`document_id`/`version`/`hash`），必须属于信封中的项目；跨项目块引用即使调用者能读两个项目也被拒绝。标题、URL 文本或摘要字符串都不构成引用。
3. `decision` 必须显式给出非空 `basis`；依据可以是资料单元（`kind="source"`）或另一条已记录的笔记版本（`kind="note"`）。模型建议、推论文本或来源标题都不能充当依据；无依据的决定返回 `evidence_required`。
4. 引用图在写入前遍历：自引、二元环或超过 `MAX_CITATION_DEPTH` 的链都以 `citation_cycle` 失败关闭。一条结论不能成为它自己的证据。
5. 笔记版本不可变。`note_revise` 追加版本且**替换**整个版本内容（未给出的字段不继承）；旧版本及其引用行原样保留，`note_status` 可按版本读回。
6. 引用状态在**读取时**推导，且不改写历史：资料单元仍是记录时的版本与内容为 `live`；file 变化/删除、文档墓碑、URL 快照刷新为 `expired`；被引笔记撤回、被修订或自身引用已过期为 `note_unavailable`。过期引用只返回状态与坐标，不返回正文。
7. 项目不再登记某 URL 时，该项目所有操作以 `registration_changed` 失败关闭，不返回缓存结论，也不改写笔记与引用行。
8. 一条笔记连同它的全部引用是一个不可分割单元：预算放不下时整条省略并计入 `omissions=["budget"]`，绝不返回只有部分证据的笔记；预算连空结果都放不下时以 `budget_too_small` 拒绝而不是截断。
9. 可见性与有效性是两件事：失去操作权限的调用在读取任何项目数据之前以 `forbidden` 失败关闭，不泄露笔记是否存在；只有有权读取却已失效的引用才在结果中说明原因。
10. 只有记录该笔记的身份可以修订或撤回它（`forbidden`）；同一项目的其他身份可以读取。笔记不修改来源、项目状态、工作目录事实或错题本。
11. 每个操作分三段执行：事务内只做数据捕获（无文件/网络 I/O）、事务外读取证据、短事务复核并提交。慢来源不占用共享写锁，文件在两段之间变化一律判为过期。
12. 没有模型调用、没有联网抓取、没有假向量：`retrieval` 恒为 `lexical`，只含脚手架词的查询没有主题，因此没有候选。

## 版本说明

- `urn:tianshu:candidate:project-research-notes:1`。`$id` 与目录名绑定；既有候选目录保持不变。
- 与既有候选相同：`arguments` 是纯数据，不声明身份；客户端身份只来自本地启动参数与独立凭据。
- 已知未覆盖：受信任的跨产品评审签名、多语言检索质量、决策的自动失效（过期只改变引用的可用性，不改变决策本身）、笔记之间的合并或拆分（当前只有不可变版本与撤回）。

## 示例

`schema.json` 与 `examples.json` 由 `tests/test_research_notes.py::test_candidate_schema_and_examples_cover_new_operations` 校验。
