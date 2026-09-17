# TS-084 研究笔记模块职责与依赖

本文是 ROUND5-MODULE-BOUNDARIES「TS-084：研究笔记」与同号任务卡的符合性记录，同时作为验收所需的「职责与依赖表」。范围只含本任务新增或修改的对象。

## 新增模块

| 模块 | 唯一负责的规则 | 数据归属 | 明确不负责 |
| --- | --- | --- | --- |
| `research_notes.py` | 研究笔记版本、修订/撤回、问题/来源论述/研究推论/未决问题/项目决策与决策依据的区别、引用闭包与循环拒绝、笔记包 seal | 只写 `research_notes`、`research_note_history`、`research_note_citations`、`research_note_index` | 资料导入/删除、来源版本与单元、项目登记与权限、项目状态、工作目录事实、错题本、模型调用、联网 |
| `research_notes_schema.py` | 笔记域表的唯一定义（含 FTS 索引与 source-guard 跟踪名单） | — | 迁移流程、事务 |
| `research_notes_migration.py` | 笔记域迁移唯一负责人：备份、前置条件、安装、失败关闭 | 只创建笔记域表 | 其他域的表、schema 3 检查点 |

## 修改的共享入口（只加必要调用与组合）

| 入口 | 修改内容 | 未做的事 |
| --- | --- | --- |
| `knowledge.py`（`KnowledgeApplication`） | 组合：`NOTE_OPERATIONS`/`NOTE_WRITE` 加入操作集合与幂等集合；`_planner` 多选一个域模块；`Plan.__call__` 路由到 `research_notes.dispatch` 并装配 `NoteActor`/`ProjectAdapter`；`_authorize` 接受笔记操作名；`ProjectAdapter`（项目表读写实现）与 `validate` 委托 | 笔记业务规则一行未写；`READ`/`WRITE`/错题本分支未改语义 |
| `validate.py`（新增） | 三个共享标量校验器（`exact`/`integer`/`string`）的唯一实现，供各域复用 | 不含任何域规则、不导入任何域模块 |
| `lessons.py`（`SourceContext`） | 新增两个窄公开端口 `observe()`、`revision_unchanged()` | 错题本自身校验逻辑未改 |
| `knowledge_migration.py` | 全新库安装时同一已审查步骤内追加 `install_research_notes` | 既有表与触发器未改 |
| `store.py` | 新增 `migrate_research_notes` 委托方法，与既有 `migrate_lessons` 同形 | Store 事务/连接/迁移职责未变 |
| `knowledge_cli.py` | 新增 `migrate-research-notes` 子命令；`action` 入口不变 | 不解析笔记语义、不直接 SQL |
| `knowledge_mcp.py` | 新增 7 个笔记工具，调用同一 `application.execute` | 不复写规则、不直接 SQL |

## 依赖方向

```
knowledge_cli / knowledge_mcp        （适配器：参数、身份、错误映射）
        ↓  只调用
KnowledgeApplication.execute          （应用用例：授权、幂等、三阶段事务）
        ↓  按域路由，注入 NoteActor + ProjectAdapter + Sources
research_notes.dispatch               （笔记域规则，叶子模块）
        ↓  ← 复用窄端口
domain / validate / SourceContext.observe / SourceContext.blocks / ProjectPort 契约 / Store.transaction
```

- `research_notes.py` 的模块级导入只有 `domain`（纯规则与错误码）、`validate`（三个共享标量校验器）与标准库；**不导入 `knowledge.py`、`lessons.py`、`app.py`、HTTP 框架或任何数据库驱动**。这一条由 AST 用例断言，因为"延迟导入"只避免加载期报错，并不消除依赖回边。
- 标量校验器放在中立的 `validate.py`：知识域与笔记域由同一条规则拒绝参数，而不必互相导入；`knowledge.py` 的 `exact`/`integer`/`string` 委托给同一实现，历史调用点不变。
- **不持有 `KnowledgeApplication`**：规则里没有 `application` 成员。身份由 `NoteActor`（冻结 DTO，二次赋值即报错）注入；项目读写由 `ProjectPort` **契约**注入，实现在拥有那些表的域里（`knowledge.ProjectAdapter`），由 `Plan` 装配。凭据留在授权代码里，只以已派生的包 seal key 进入本模块。
- 资料单元核验经由应用已有的证据端口（`Sources.context()` → `SourceContext.observe/blocks`），没有第二套来源库、权限缓存或哈希实现。
- 反向依赖：`domain`、`validate`、`store`、`lessons` 不导入 `research_notes`；`knowledge.py` 只在 `_planner`/`Plan` 内按操作名延迟导入该模块，用于选择 schema 门与装配端口，不复制任何笔记规则。
- 没有共享连接、全局变量、跨工作树导入或私有成员访问：笔记模块不使用 `Evidence._revision`、`Plan.pending`、`Store._connect` 等私有成员。

## 注入端口

| 端口 | 提供者 | 内容 | 笔记域不得做的事 |
| --- | --- | --- | --- |
| `NoteActor(client)` | `Plan.__call__`（身份在授权后解析一次） | 冻结的操作者身份，用于"只有记录者能改" | 不从全局或应用对象读身份；不接触凭据；不重新赋值 |
| `ProjectPort` 契约（`revision()`/`declared_state()`/`bump()`） | 契约在笔记域声明；实现是 `knowledge.ProjectAdapter`，由 `Plan` 注入 | 读项目版本、读项目声明状态、笔记写入提交后递增项目版本 | 不写项目表 SQL、不导入路由模块、不使用契约之外的方法 |
| `Sources.context(db, project_id, phase)` | 既有知识域 | 资料单元核验、项目 revision 未变判定、捕获段期望记录 | 不另造来源核验、权限缓存或哈希 |
| `research_notes.snapshot` | 应用按域交接 | schema/登记门（`metadata` + `knowledge_projects` 登记比对） | 不在业务规则内重复该检查 |

可执行证据：`tests/test_research_notes.py::test_the_note_module_keeps_its_declared_boundaries` 解析模块 AST，断言：规则代码里没有 `application` 属性访问；模块不导入 `knowledge`；除 `snapshot` 门以外没有任何 `knowledge_projects` 查询；`ProjectPort` 契约内没有存储实现；`knowledge.ProjectAdapter` 才是持有项目表 SQL 的一方且由 `Plan` 注入；`NoteActor` 赋值或删除即报错。

## 引用图的写入前核验

| 项 | 规则 |
| --- | --- |
| 环判定 | 只有**回边**（在被遍历路径上再次遇到）才是环：自引、二元环拒绝；菱形共享祖先不是环，第二分支去重即可 |
| 深度上限 | 根记为深度 1，路径超过 `MAX_CITATION_DEPTH`（64）即拒绝，而不是"走到上限就接受" |
| 工作量上限 | 单次遍历最多访问 `MAX_GRAPH_WORK`（4096）个节点；超预算同样拒绝，绝不用部分遍历的结论回答 |
| **边的版本** | 遍历按**每条引用边记录的** `(note_id, version, hash)` 前进并逐边核验：被引版本必须是当前 `ready` 版本、`research_notes.version` 等于该版本、重建指纹等于该边记录的 `hash`。**绝不用被引笔记的当前版本替代历史引用** |
| 传递来源依赖 | 只用**被核验过的那些版本**收集资料单元，与直接引用的单元在同一阶段核验：捕获段只记录期望（事务内、无文件 I/O），提交段逐条严格核验，因此被顶替或过期的链在写入前就被拒绝 |
| 历史 | 拒绝只影响新写入；已存储版本与其引用行一律保持原样 |

## 接口与状态所有者

| 操作 | 输入 | 输出 | 状态所有者 | 幂等键 | 权限 |
| --- | --- | --- | --- | --- | --- |
| `note_record` | `key`、`expected_version=0`、`note` | `note_id`/`version`/`hash`/引用计数 | 笔记域（`research_notes`） | `dedupe`（缺省 `key`） | `note_record` |
| `note_revise` | `key`、`note_id`、`expected_version`、`note` | 新 `version`/`hash` | 同上；记录者身份 `owner` | `dedupe` | `note_revise` |
| `note_withdraw` | `key`、`note_id`、`expected_version`、`reason` | `status`/`version`/`effect` | 同上 | `dedupe`（缺省 `key`） | `note_withdraw` |
| `note_query` | `text`、`budget_bytes` | `notes`/`omissions`/引用状态 | 只读 | — | `note_query` |
| `note_recover` | `text`、`budget_bytes` | 短包 + `seal`/`revision` | 只读 | — | `note_recover` |
| `note_status` | `note_id`、`version` | 该版本 + 引用状态 + `current_version` | 只读 | — | `note_status` |
| `note_check` | `package` | `valid`/`reason` | 只读 | — | `note_check` |

- 资料版本、单元、项目登记、权限、项目 revision 的状态所有者仍是既有 Knowledge 能力；笔记只保存引用与自己的版本。
- 笔记版本不可变：修订追加版本，历史版本及其引用行保持原样；撤回追加一个标记版本并让笔记离开查询索引。
- 每个操作的 `expected_version`、幂等结果、引用过期判定都在三阶段事务边界内完成：捕获段只做本地读取并记录期望，外部段读文件，提交段只比较记录结果。共享写锁不跨文件系统 I/O。

## 禁止关系（已核对）

| 禁止项 | 核对结果 |
| --- | --- |
| 再造一套来源库或权限缓存 | 无：引用核验全部经 `SourceContext` 与 `KnowledgeApplication` 授权 |
| 笔记规则持有整个应用对象 | 无：身份与项目读写由 `NoteActor`/`ProjectPort` 注入，规则代码内无 `application` 访问（AST 用例断言） |
| 笔记域导入路由模块（依赖回边） | 无：`research_notes.py` 不导入 `knowledge`，标量校验走中立 `validate.py`（AST 用例断言）；`knowledge` 只在 `_planner`/`Plan` 内按操作名延迟导入笔记模块 |
| 消费方模块包装其他域的表 SQL | 无：`ProjectPort` 只是契约（无存储实现），项目表 SQL 在 `knowledge.ProjectAdapter` 并由 `Plan` 注入（AST 用例断言） |
| 笔记规则直接读写其他知识域表 | 无：规则内的 SQL 只出现在笔记域表；唯一例外是 `snapshot` 门的登记比对，且只有它查询 `knowledge_projects`（AST 用例断言） |
| CLI/MCP 分别实现规则或入口直接 SQL | 无：两个入口只做参数、身份、错误映射，规则只在 `research_notes.py` |
| 笔记擅自修改源、项目状态、工作目录事实、错题本 | 无：`test_the_note_module_writes_only_its_own_tables` 按表计数实测 |
| 反向控制 recover 流程 | 无：接续使用独立的 `note_recover` 有界只读包，不改 `continuation_recover` |
| 假身份走聊天 SourceAuthority | 无：仍由 `KnowledgeApplication.execute` 的 client/credential 授权 |
| 循环引用充当自己的证据 | 拒绝：`citation_cycle`（自引、回边环、超过深度或工作量上限的遍历） |
| 过期链上的新决定被接受 | 拒绝：被引笔记的传递资料依赖在同一阶段核验，写入前即 `stale_evidence` |
| **被顶替的中间引用被当前版本顶替** | 拒绝：每条边按记录的 `(note_id, version, hash)` 核验，只用被核实版本收集资料单元；实测 A 修订到 v2 后引用 A v1 的记录与修订都被拒绝 |
| 撤回笔记在链中被当作可用 | 拒绝：链上任一版本 `state != ready` 即 `stale_evidence` |
| 共享祖先被误判为环 | 无：菱形与宽扇入实测可写入且读回为当前 |
| 模型建议自动升级为决定 | 无模型调用；决定必须显式给出 `summary` 与非空 `basis` |
| 旧结论静默重新标当前 | 读取时按当前来源状态推导 `current`/`citation_states`，不改写历史版本 |
| 失权正文泄漏 | 过期引用只返回状态与引用坐标，不返回正文（`test_a_deleted_source_expires_the_citation_and_leaks_no_text`） |

## 迁移

| 项 | 值 |
| --- | --- |
| 元数据标记 | `research_notes_schema=1`（schema 3 之上） |
| 表归属 | 知识笔记域：`research_notes`、`research_note_history`、`research_note_citations`、`research_note_index` |
| source-guard | 前三张表全部注册 `source_revision_*` 触发器（`research_note_index` 为 FTS 派生索引，与其他域一致不入跟踪名单） |
| 全新库 | `migrate` 在同一已审查步骤内安装（含笔记域） |
| 已有库 | `migrate-research-notes --backup <不存在的路径>`，先停写；缺少标记时所有笔记操作以 `dependency_unavailable` 失败关闭 |
| 失败恢复 | 迁移前完整数据库保留在备份路径；已升级库再次迁移被拒绝；不重建、不覆盖 schema 3 检查点 |
