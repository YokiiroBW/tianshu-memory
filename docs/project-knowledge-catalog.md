# 项目资料目录与完整语义块分页阅读

本文件描述两个只读操作：`document_list`（项目资料目录）与 `document_read`（单文档完整语义块分页阅读）。
它们**不是**检索引擎：`query` 按关键词返回命中的块，这两个操作按稳定键序返回**目录**与**某一份文档的
全部块**，因此调用方不必先猜关键词就能枚举一个项目里已导入的资料，并按 `document_id` 逐份读完。

两者是产品内部的受限接口候选，**不是已发布的跨产品合同**。本轮不写网页、不选前端引擎、不改锁、不接
平台身份、不新增依赖。

## 1. 位置

| 层 | 文件 | 职责 |
|---|---|---|
| 领域 | `src/tianshu_memory/knowledge_catalog.py` | 常量、游标签名、预算装配、两条 keyset SQL、`Catalog` 三阶段派发、唯一窄端口 |
| 领域接线 | `src/tianshu_memory/knowledge.py` | `READ` 增两名、`CATALOG_OPERATIONS`、planner 路由、`Plan` 委托、窄 `Evidence` 端口注入 |
| 迁移 | `src/tianshu_memory/knowledge_catalog_migration.py` | `VERSION`、两条索引 DDL、`install(db)`、`migrate(store, backup_path)` |
| 全新库 | `src/tianshu_memory/knowledge_migration.py` | 在同一个迁移事务内调用同一个 `install` |
| 命令 | `src/tianshu_memory/knowledge_cli.py` | 新增 `migrate-catalog --backup <路径>` |
| HTTP | `src/tianshu_memory/knowledge_http.py` | `ALLOWED_OPERATIONS` 由十项增至十二项；准入、身份与状态矩阵一字未改 |

`store.py` 与 `source_recovery.py` **零改动**：迁移复用既有 `Store.transaction()` 与 schema 3 的
`source-guard.json` 检查点，不新增任何 `Store` 方法。

## 2. 参数与返回（严格）

两个操作的参数都是**封闭集合**：多一个字段、少一个字段都是 `invalid_input`（领域 422）。分页三参数
为 `limit`、`budget_bytes`、`cursor`，其中 `cursor` 必须显式给出（首屏写 `null`）。

### `document_list`

```json
{"operation": "document_list", "project_id": "alpha",
 "arguments": {"limit": 8, "budget_bytes": 32768, "cursor": null}}
```

```json
{
  "project_id": "alpha",
  "project_revision": 0,
  "items": [
    {"document_id": "document:6f2a…", "kind": "file", "version": 1,
     "indexed_state": "ready", "source_validation": "not_checked"}
  ],
  "next_cursor": "eyJ…",
  "omissions": [],
  "trust": "source_material_not_instructions"
}
```

`items` 严格为 `{document_id,kind,version,indexed_state,source_validation}`，按 `document_id` 升序稳定排列。
`indexed_state` 是**导入时**记录的索引状态；`source_validation` 恒为 `not_checked`——目录是索引描述，
**从不声称来源文件当前仍然有效**，这一点必须由调用方按需用 `document_read` 或 `check` 复核。

响应字段就是卡里点名的那几个：`limit` 属于**请求**（也属于游标绑定），不属于响应。调用方自己知道这一页
是按哪个元素上限要来的，多返回一个未冻结的字段只会让后来的读者把它当成合同。

### `document_read`

```json
{"operation": "document_read", "project_id": "alpha",
 "arguments": {"document_id": "document:6f2a…", "expected_version": 1,
               "expected_hash": null, "limit": 8, "budget_bytes": 32768, "cursor": null}}
```

```json
{
  "project_id": "alpha",
  "project_revision": 0,
  "document_id": "document:6f2a…",
  "version": 1,
  "hash": "9c1d…",
  "blocks": [
    {"reference": {"block_id": "document:6f2a…:1:000", "document_id": "document:6f2a…",
                   "version": 1, "hash": "9c1d…"},
     "text": "完整语义块正文", "spans": [[1, 1]]}
  ],
  "next_cursor": null,
  "omissions": [],
  "trust": "source_material_not_instructions"
}
```

每块只有 `{reference{block_id,document_id,version,hash},text,spans}`：**只返回完整块**，绝不返回半块或
截断正文。`expected_version` 必填；`expected_hash` 可为 `null`，给定时必须与当前版本摘要一致，否则
`stale_evidence`。返回的 `hash` 是调用方续页时要带回的值。

**续页前提由调用方给出，游标只重复它、不替代它**：非首屏（`cursor` 非 `null`）时 `expected_hash` 必填，
`expected_version`/`expected_hash` 必须与游标封存的 document_id/version/hash 完全一致。缺摘要为
`invalid_input`（领域 400）；与游标矛盾为 `invalid_cursor`（领域 400）——游标**不会**自动纠正调用方
写错的版本或摘要，也不会因为调用方指向另一版本就继续把旧版本正文发出去。文档本身的当前版本已经改变
（调用方的前提与库内事实不符）仍是 `stale_evidence`。

### 两者都不返回

`root`、`host`、`locator`、绝对路径、URL 的 query 部分、`provenance`、其他项目的任何标识、凭据摘要、
文件系统或 SQL 细节。没有标题字段的资料只有 `document_id` 作为身份——不猜标题、不合成摘要。

### 固定边界

| 参数 | 范围 | 越界 |
|---|---|---|
| `limit` | 整数 1–32 | 领域 `invalid_input` |
| `budget_bytes` | 整数 1024–32768 | 领域 `invalid_input` |
| `cursor` | 字符串 ≤ 2048 字符 | `invalid_cursor` |
| `document_id` | 字符串 ≤ 128 字符 | 领域 `invalid_input` |
| `expected_version` | 整数 1–2³¹ | 领域 `invalid_input` |

## 3. 字节预算

预算的对象是**整个响应序列化后的 UTF-8 字节数**（含 `next_cursor`、`omissions`、中文正文与 JSON 结构），
按 `canonical`（确定 JSON，非 ASCII 转义）计算。因此中文块与转义字符都按**真实字节**计入，而不是按
字符数估计。

装配规则（`page()`，两个操作共用）：预算按**这一页真实要发的那个响应**计算——有下一页才有游标，
末页既没有 `next_cursor` 也不会有 `budget` 省略。

1. 逐个元素试算"整包（含这一页真会带的游标与省略）是否放得下"，只交付**真正放得下**的前缀；
2. **候选恰好到此结束**（`limit` 已覆盖全部候选）时，整页的响应**不带游标**：先按这个形状试算，放得下就
   整页交付，`next_cursor: null`、`omissions: []`。**不因为一个根本不会返回的游标而拒绝一个放得下的末页**
   ——单块完整末页（约 818 字节）在最小预算 1024 下必须成功；
3. 第一个元素连"带游标"都放不下 → `budget_too_small`（领域 400），**不给游标**。调用方必须提高
   `budget_bytes` 重试；本操作不跳过放不下的元素，也不悄悄丢掉目录开头（游标真的放不下时不假装能续页）；
4. 预算截断且仍有剩余元素 → 交付已放下的前缀 + `next_cursor` + `omissions == ["budget"]`；
5. 元素个数达到 `limit`、或候选恰好到此结束 → 只有在**确实还有剩余**时给 `next_cursor`，且
   `omissions == []`——整页不是预算省略，绝不虚报。

`omissions` 只有 `[]` 与 `["budget"]` 两种取值。空目录、零块文档返回**空列表 + `next_cursor: null`**，
不伪造内容，也不把"没有资料"说成错误。

## 4. 游标

游标是**签名**的页位置，不是可编辑的 offset：

- 内容：确定 JSON（版本号 + 独立 purpose `knowledge-catalog-cursor` + 绑定字段），base64url 编码，限长
  2048 字符；
- 签名：HMAC-SHA256，密钥是该次派发的既有项目 `seal_key`（由项目 snapshot 的 `knowledge_seal_key`
  与 `[client, secret]` 派生），**不引入任何新的数据库签名秘密**；
- 绑定字段：`operation`、`client`、`project_id`、`revision`、`limit`、`budget_bytes`，以及读操作的
  `document_id`/`version`/`hash`，加上 `last_id`（本页真正交付的最后一个元素）。非首屏 `last_id` 必有。

因此一个游标**只能**由同一个身份、同一个项目、同一个操作、同样的 `limit`/`budget_bytes` 继续使用；
改 `limit`、改预算、跨操作、跨 client、跨项目或改一个字节都得到 `invalid_cursor`（领域 400）。

绑定是**双向**的：游标封存的 `document_id`/`version`/`hash` 必须与调用方这一请求自己给的
`expected_version`/`expected_hash` 一致，否则是 `invalid_cursor`。游标的职责是证明"上一页读的是哪一份"，
不是替调用方决定"这一页该读哪一份"——用游标覆盖请求前提，就等于调用方指向另一版本时仍把旧版本正文
发出去。

已验签但**页间项目 revision 变了** → `cursor_stale`（领域 409）：目录已经动了，调用方要从第一页重新
开始，而不是被交给一次过期遍历的剩余部分。同一次请求的三阶段之间变了 → 既有的 `project_conflict`。

## 5. 权限与身份

- 两项各需**同名显式 permission**：`document_list`、`document_read`，由既有 `knowledge.clients` 逐操作
  列表判定，不新增身份路径、不自动授权、不修改任何现有客户端配置。
- `query`、`recover` **不隐含**枚举或读取能力：只有 `query` 权限的旧 reader 调用这两个操作得到 422
  `forbidden`（授权判定在数据库存在性判断之前）。
- 未授权项目在任何数据库判断之前就拒绝；未登记项目 → `project_uninitialized`（409）。**读操作从不
  注册项目**。
- 撤权或凭据轮换后，后续请求（含续页）失败关闭。

## 6. 证据规则

- `document_list` **不读任何来源文件**。它读的是索引行；`source_validation` 恒 `not_checked`。
- `document_read` 每页都**重新核对来源当前性**：文档存在、版本与调用方一致、摘要一致、状态可读、
  文件型来源经领域自身 reader 重新读取并与记录的摘要一致。任一不成立 → `stale_evidence`（领域 409），
  **不返回任何旧正文**。
- URL 型文档是显式导入留下的快照：本操作**从不访问网络**；使 URL 文档不再当前的，是它的 URL 从项目
  登记中撤出，而不是一次抓取失败。
- 来源文件被删除、改写、或读取期间正在变化时，reader 报告 `None`，同样归入 `stale_evidence`。

## 7. 事务与三阶段

两个操作走项目域既有的三阶段，而不是自己开事务：

1. **capture**（持锁，不做 I/O）：schema 门槛、游标校验、项目 revision 与登记核对；`document_read`
   在此**只记录**它这一页需要复核的那一份来源（登记 reader 期望），`capture` 阶段的返回值不交给调用方；
2. **external**（不持锁）：重新读取那一份来源文件（仅 `document_read`）；
3. **serve**（持锁）：再核对 schema 与项目 revision、比较来源新鲜度，然后才装配整页。

因此长事务里没有文件 I/O，读操作不写任何行、不推进项目 revision、不写幂等账本，也不调用任何 snapshot。
并发导入使版本前移时，续页的旧 `document_id`/`version`/`hash` 对不上即拒绝。

## 8. 冻结索引与查询计划

两条最小索引（名字即 schema 的一部分）：

```sql
CREATE INDEX knowledge_documents_project_id ON knowledge_documents(project_id,id);
CREATE INDEX knowledge_blocks_document_version_id ON knowledge_blocks(document_id,version,id);
```

- 两条语句**点名索引**（`INDEXED BY`），并由**同一次请求内的形状自检**兜底：`snapshot` 与 `Catalog`
  都先运行 `inspect`，索引缺失或同名但形状不对 → `dependency_unavailable`（503），绝不退化为全表扫描。
  之所以点名：键集从表头附近开始时，放任 planner 可能选 `id` 主键再跨项目过滤，成本随**别的项目**增长。
- `id` 下界**恒为字符串**（首屏为空串，任何文档 id 都不可能等于它）。写成 `(? IS NULL OR id>?)` 读起来
  一样，对 SQLite 却不一样：那样只用得上 `project_id`，随后逐行过滤。单值单比较才能让一页的成本与
  **这一页**成正比。
- `inspect` 判定的是**完整形状**，不是名字：`sqlite_master` 里那条建索引语句（只忽略空白与大小写）、
  表名、`PRAGMA index_info` 的列与列序、`PRAGMA index_list` 的 unique/partial 标志。同名错列的索引会被
  拒绝而不是被信任——列序换了、多一列、加了 `UNIQUE`、加了 `WHERE`、加了 `COLLATE` 都算不同定义。

## 9. 迁移与回滚

### 命令

```powershell
uv run python -m tianshu_memory.knowledge_cli `
  --config C:/private/memory-knowledge.json `
  migrate-catalog --backup C:/private/backups/catalog-before.sqlite
```

全新数据库不需要单独跑这一步：`migrate-knowledge` 在它自己的迁移事务里调用**同一个** `install`，所以
全新库与已装库拿到的是同一份（逐字节相同的）索引定义。

### 门槛与动作

前置：`schema == 3` 且 `knowledge_schema == 1`，且尚无 `knowledge_catalog_schema`。已装库再跑一次 →
`ValueError: Catalog migration already applied`（并保留它自己那份独占备份）。

动作顺序：**先**以 `xb` 独占方式占住一个本地备份文件（拒绝覆盖既有回滚产物、拒绝 UNC 路径、拒绝与库或
检查点同文件）→ 进入既有 `Store.transaction()` → 事务内把完整迁移前数据库备份到该文件 → **形状前置检查**
（下面"错误同名索引"）→ `install`：只创建**缺失**的索引、`PRAGMA index_info` 自检、写
`metadata.knowledge_catalog_schema=1`、显式 `source_revision + 1`（这一步让既有 Store 事务推进 schema 3 的
`source-guard.json` 检查点）。

**错误同名索引 → 失败关闭，绝不 DROP 重修**：迁移前若同名索引已存在但不是本版本评审过的定义，整个升级
在写任何东西之前拒绝（`ValueError: Catalog index already exists with a different definition: <名字>`），
保留原索引、原 metadata、原 `source_revision` 与 guard 检查点不动。**形状正确**的既有索引被**复用**（不
DROP、不重建，B-tree 根页不变）；**缺失**的索引在同一个迁移事务里按定义创建。本版本不认识的 schema 由
人处理，不由迁移器"顺手修好"。

不新建表、不新建触发器、不动任何文档/版本/块/项目 revision/客户端权限——索引不是权威行。

### 失败与回滚

- 命令以一行结果体回答：成功是迁移返回的字典（`schema`/`knowledge_schema`/
  `knowledge_catalog_schema`/`indexes`/`backup`），失败是
  `{"status":"failed","code":"dependency_or_input_error"}` 与退出码 1——**不打印原始异常**，重复升级、
  缺前置 schema、备份文件已存在都走这一条。
- 缺 `source-guard.json`：读取先失败关闭（`dependency_unavailable`），备份已生成，不重建检查点。
- 检查点与数据库不一致：同样失败关闭，工具**不覆盖**检查点来"修复"。
- 中断/失败：事务原子回滚，检查点保持原状态，备份保留供审查（`.runtime` 之外由操作者保管）。
- 回滚**不是**把旧备份直接盖回去：旧备份的 `source_revision` 落后于新检查点，会被拒绝。正确顺序是
  **停止写入 → 恢复审查 → 按 `docs/source-sync-runtime.md` 的规则处理**。
- 运行期：未升级的库上这两个操作失败关闭为 `dependency_unavailable`（503），而 `query` 等旧操作在同一
  个库上**继续可用**。

## 10. 验收

```powershell
uv run pytest tests/test_knowledge_catalog.py tests/test_knowledge_catalog_http.py -q --basetemp .runtime/tests-ts086-catalog
uv run pytest tests/test_knowledge_catalog_migration.py -q --basetemp .runtime/tests-ts086-migration
```

- `test_knowledge_catalog.py`：领域验收（稳定翻页、末页、空目录、零块文档、逐块完整、列表不触源文件、
  来源变动/删除/版本/URL 撤出、权限独立、伪造身份、撤权与轮换、游标绑定、预算与 `budget_too_small`、
  参数封闭、无权威写入、并发、guard 故障、无迁移库、错索引、迁移保值、UTF-8 字节预算）。
- `test_knowledge_catalog_http.py`：真实 ASGI 入口（真实 `response.content` 字节预算、真实 HTTP 状态与
  媒体类型、跨项目与伪造身份、游标绑定、来源变动、未升级库失败关闭），以及一个**真实子进程**用例：
  启动 `knowledge_cli serve`、经真实 socket 走"列表 → 阅读列表指名的文档 → 下一页"，并与进程内入口
  在同一库上的答案逐字比对。
- `test_knowledge_catalog_migration.py`：迁移验收（全新/已装库、重复迁移、缺 guard、错误 guard、已存在
  备份、六种错误索引定义与一种只是排版不同的等价定义、缺 `knowledge_schema`、备份即迁移前完整库、迁移不
  动权威行/触发器/权限，以及 `EXPLAIN QUERY PLAN` 与 `progress_handler` 的规模对比）。其中两项**真实启动**
  操作者命令
  `python -m tianshu_memory.knowledge_cli --config <私有配置> migrate-catalog --backup <新文件>`：一项核对
  子进程留下的备份、两条索引、一条版本行，并随后经公共 `KnowledgeApplication.execute` 用两个操作读回
  升级前就导入的那份资料；另一项核对重复升级被拒（命令以 `{"status":"failed",
  "code":"dependency_or_input_error"}` 与退出码 1 结束，不打印原始异常）、它自己的独占备份仍被保留、
  且没有写入第二条版本行。

本任务的真实 socket 与 ASGI 替身、静态检查都分别记录在 `docs/handoffs/TS-086.md`；不把 ASGI 替身写成
真实 socket 通过，也不把隔离 SQLite 写成生产迁移通过。

## 11. 与本轮任务卡的差异（如实列出）

1. **游标同时绑定 `budget_bytes`**：卡只点名 `limit`；实现把预算一并封入游标，改预算续页即
   `invalid_cursor`，因为一页按一个预算装配，不该被换个预算重放后还声称遵守了它。响应**不**包含 `limit`
   字段——`limit` 只属于请求与游标绑定（首轮实现曾多返回 `limit`，已按验收意见移除）。
2. **两条 SQL 点名索引（`INDEXED BY`）**：卡要求计划前缀 `SEARCH`、无临时排序；实测放任 planner 在键集
   靠近表头时可能改走 `id` 主键再过滤 `project_id`，故语句点名索引，并由同一次请求内的形状自检兜底
   （缺失/错形状 → `dependency_unavailable`）。
3. **跨项目 `document_read` 是 `stale_evidence`**：与本项目域既有证据规则一致，且拒绝体不报告该文档是否
   存在于别处。
