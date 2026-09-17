# 研究笔记与项目决策的版本证据（TS-084）

在 TS-080 项目资料域与 TS-081 错题本之上追加**有版本的研究笔记**与**显式的项目决策**。复用同一 SQLite Store、schema 3 source-guard、`KnowledgeApplication` 的配置授权、三阶段事务边界、完整语义单元引用与字节预算；不调用模型、不联网、不监听外部客户端、不自动把推论升级为决定。

模块职责、依赖方向、数据归属与禁止关系见 [研究笔记模块职责与依赖](project-research-notes-boundaries.md)。

## 迁移与配置

笔记表属于知识域的独立可选子域，有两种安装方式：

- 全新数据库：`migrate` 在同一个已审查步骤里安装资料、错题、目录计划与笔记对象。
- 已有资料库（`knowledge_schema=1` 且无 `research_notes_schema`）：显式升级，必须先停写并备份。

```powershell
uv run python -m tianshu_memory.knowledge_cli --config C:/private/memory.json migrate-research-notes --backup C:/private/before-notes.sqlite
```

缺少 `research_notes_schema` 时所有笔记操作以 `dependency_unavailable` 失败关闭。迁移保留迁移前完整数据库；备份路径必须不存在，已升级的库再次迁移被拒绝。新增权威表 `research_notes`、`research_note_history`、`research_note_citations` 全部带 `source_revision` 触发器；不重建或覆盖 schema 3 检查点，旧库恢复仍被 guard 拒绝。

权限沿用同一份 `knowledge.clients`，操作名即权限名：

```json
{
  "knowledge": {
    "projects": {
      "alpha": { "root": "C:/isolated/alpha", "host": "local", "default_branch": "main", "urls": [] }
    },
    "clients": {
      "alpha-writer": {
        "credential_sha256": "<sha256>",
        "projects": ["alpha"],
        "permissions": ["import", "query", "note_record", "note_revise", "note_withdraw", "note_query", "note_recover", "note_status", "note_check"]
      },
      "alpha-reader": {
        "credential_sha256": "<sha256>",
        "projects": ["alpha"],
        "permissions": ["query", "note_query", "note_recover", "note_status", "note_check"]
      }
    }
  }
}
```

项目写权限、错题本权限、服务令牌或来源受理都不构成笔记权限；缺少操作权限的调用在读取任何项目数据之前以 `forbidden` 失败关闭。

## 六类内容明确分开

`note_record` 录入一条有版本的研究笔记。一次请求里六类内容各有自己的字段，服务不会把它们合并：

```json
{"operation":"note_record","project_id":"alpha","arguments":{"key":"receipt-retry","expected_version":0,
 "note":{
  "question":"重试前是否必须先确认回执？",
  "source_statements":[
   {"kind":"claim","statement":"来源认为重发前必须确认回执。",
    "source":{"block_id":"…","document_id":"…","version":1,"hash":"…"}},
   {"kind":"limitation","statement":"该来源只覆盖单机场景。",
    "source":{"block_id":"…","document_id":"…","version":1,"hash":"…"}}],
  "inferences":["等待回执与观察到的重复投递一致。"],
  "open_questions":["分区期间回执表是否权威尚未确定。"],
  "decision":{"summary":"本项目决定重试路径先确认回执。",
   "basis":[{"kind":"source","reference":{"block_id":"…","document_id":"…","version":1,"hash":"…"}}]}}}}
```

- `question` 是本次研究问题；`source_statements` 是**来源论述**，每条的 `statement` 是操作者自己的话，`source` 是**实际读到的**已登记资料版本与完整语义单元引用；`inferences` 是**研究推论**；`open_questions` 是**未决问题**。
- `decision` 是**项目决策**，必须显式给出 `summary` 与非空 `basis`（**决策依据**）。依据可以是资料单元（`kind="source"`）或另一条已记录的笔记版本（`kind="note"`）。没有依据的决定被 `evidence_required` 拒绝；模型建议、推论文本或来源标题都不能充当依据。
- 引用不接受标题、URL 文本或摘要字符串：必须是与本项目 `query` 当前返回一致的块引用，否则 `not_found`/`stale_evidence`。跨项目块即使调用者能读两个项目也被拒绝。
- **被引笔记的整条依赖链一并核验，且按每条边记录的版本核验**：一条结论只有在它引用的每个资料单元都仍然有效时才是证据，因此新决定会检查被引笔记传递闭包里的所有资料单元。遍历按**引用边记录的** `(note_id, version, hash)` 前进——被引笔记后来被修订，不会让它变成历史引用所指的证据——每条边都要求被引版本仍是当前 `ready` 版本、版本号相符、重建指纹相符，并且只用这些**被核实版本**收集资料单元。链上任何一环被顶替、撤回或过期都在**写入前**以 `stale_evidence` 拒绝，而不是先提交再在读取时标为不可用。历史版本不受影响。
- `key` 是笔记的稳定身份：`note_id = "note:" + fingerprint([project_id, key])`，版本从 1 开始。身份只创建一次，之后只能通过 `note_revise` 增长；重复录入是 `version_conflict`。
- 只有记录该笔记的身份可以修订或撤回它（`forbidden`）；同一项目的其他身份可以读取。

## 修订、撤回与历史

- `note_revise` 追加新版本，要求 `expected_version` 等于当前版本，并复用同一 `key`（否则 `identity_conflict`）。版本不可变：旧版本连同它的引用行原样保留，`note_status` 可按版本读回。
- 修订会替换整个版本内容，未在新请求中给出的字段（例如 `decision`）不会自动继承。
- `note_withdraw` 记录撤回：追加一个带 `withdrawn.reason` 的版本，笔记离开查询索引，引用它的笔记不再把该版本当作当前证据。撤回后不能修订回活（`already_withdrawn`），不能重复撤回。
- 一个 `key` 只能撤回一次，因此需要新的 `dedupe`；相同幂等键同负载重放返回历史结果，异负载 `idempotency_conflict`。

## 引用状态与失权

每个笔记版本的引用在**读取时**重新判定，不做后台改写、不把正文复制到笔记：

| 情形 | 引用状态 | 行为 |
| --- | --- | --- |
| 资料单元仍是记录时的版本与内容 | `live` | `current: true` |
| file 证据被修改/删除，文档被删除或标记不可用，URL 快照被刷新到新版本 | `expired` | `current: false`，引用仍列出坐标但**不返回正文** |
| 项目不再登记该 URL | 该项目的所有操作以 `registration_changed` 失败关闭，笔记与引用行不被改写 | 不返回缓存结论 |
| 被引用的笔记被撤回、被修订到新版本，或它的引用已过期 | `note_unavailable` | `current: false`；不能作为新决定的依据（`stale_evidence`） |
| 调用者失去操作权限 | 不适用 | 整个请求以 `forbidden` 失败关闭，不泄露笔记是否存在 |

`note_query` 返回的每条笔记都带完整 `citations`（序号、种类、引用坐标、状态）与 `citation_states`。旧结论因此始终可以追溯到当时读到的版本，同时不会被静默重新标为当前。

## 循环引用

决定依据里引用另一条笔记版本会形成引用图。服务在写入前遍历该图，并且**按每条边记录的版本**遍历：

- 笔记引用自己 → `citation_cycle`；
- **回边**（在被遍历的路径上再次遇到某条笔记，例如两条笔记互相引用）→ `citation_cycle`；
- 链长超过 `MAX_CITATION_DEPTH`（64，根记为深度 1）→ 同样 `citation_cycle` 失败关闭，而不是"走到上限就接受"；
- 一次遍历访问超过 `MAX_GRAPH_WORK`（4096）个节点 → 同样 `citation_cycle`，绝不用部分遍历的结论回答。

**菱形不是环**：两条研究各自引用同一份基础笔记、再由第三条把它们合起来，是正常的证据结构——共享祖先在第一条分支上走完，在第二条分支上去重，不判为环。同理，多条笔记引用同一条综合结论也不构成环。

**边指向的是版本，不是身份**：引用 A v1 的笔记在 A 修订到 v2 之后不再当前，此时任何以它为证据的新记录或修订都会被 `stale_evidence` 拒绝——遍历不会用 A v2 代替 A v1。撤回同理。**每条边独立核验，核验先于去重**：依据里同时出现 A v2 与"引用 A v1 的笔记"时（无论两者顺序如何），旧边同样被拒绝；已核验过的身份只免去重复展开其后代，不免除后续边的核验。

因此一条结论永远不能成为它自己的证据，共享证据的正常综合不会被误杀，被顶替或撤回的中间结论也不会被悄悄当成可用证据。

## 查询、预算与接续

`note_query` 参数 `text`、`budget_bytes`（256–32768），只在本项目 FTS 索引上检索当前 `ready` 笔记：

- 一条笔记**连同它的全部引用**是一个不可分割单元。预算放不下时整条省略并计入 `omissions=["budget"]`，绝不返回只有部分证据的笔记。预算连空结果都放不下时以 `budget_too_small` 拒绝，而不是截断。
- 过期或失权的引用只贡献状态与坐标，不贡献正文。
- `retrieval: "lexical"`：没有向量、没有模型、没有假成功。只含脚手架词的查询没有主题，因此没有候选。

`note_recover` 返回同一批笔记加当前项目状态（`goal`/`unfinished`）与 `revision`/`registration`/`seal` 的短包，预算 1024–32768。包与服务端绑定：替换、删除、修订、撤回、项目状态更新、配置变化或内容篡改都会使旧包失效；复用前必须 `note_check`。

接续边界：

- 笔记是独立的有界只读结果，`continuation_recover` 不被笔记模块反向控制，也不会把笔记注入到工作目录事实里。
- `note_recover` **不含** `worktree`/`branch`/`head` 等字段：项目当前工作目录事实仍由接续包拥有并优先于任何笔记叙述。
- 笔记不常驻注入：只有显式查询命中的笔记才返回。

## 命令与验证

CLI 与 MCP 入口与 TS-080/TS-081 相同，新增 `migrate-research-notes` 子命令与 7 个笔记工具（服务共 29 个工具）。

**幂等键在入口处的形状**：`key` 是笔记的稳定身份，因此对同一身份的任何后续操作都需要它自己的操作键 `dedupe`。三个写工具（`note_record`、`note_revise`、`note_withdraw`）都接受可选 `dedupe`，省略时退化为 `key`：

- 通过 MCP 修订一条已记录的笔记时必须给出 `dedupe`：不给会落到记录时的键上，被 `idempotency_conflict` 拒绝（同一个键绑定了另一个请求）；换一个新的 `key` 则被 `identity_conflict` 拒绝。`note_revise`/`note_withdraw` 因此各自需要独立键，同键同负载重放返回记录结果。

```powershell
uv run python -m tianshu_memory.knowledge_cli --config C:/private/memory.json action --client alpha-writer --credential-env TIANSHU_PROJECT_SECRET C:/private/note.json
uv run --extra mcp python -m tianshu_memory.knowledge_cli --config C:/private/memory.json mcp --client alpha-writer --credential-env TIANSHU_PROJECT_SECRET
```

```powershell
uv run --extra mcp pytest tests/test_research_notes.py tests/test_research_notes_transport.py -q --basetemp .runtime/tests-ts084-targeted
uv run --extra mcp pytest -q --basetemp .runtime/tests-ts084
```

专项与全产品实际结果见 `docs/handoffs/TS-084.md`。协议候选见 `docs/candidates/project-research-notes/v1/`，仍未发布为根合同。
