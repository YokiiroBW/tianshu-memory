# 项目错题本与总经验（TS-081）

在 TS-080 的项目资料域上追加**有版本的项目错题**与**显式批准的总经验**。复用同一 SQLite Store、schema 3 source-guard、`KnowledgeApplication` 的配置授权和三阶段事务边界；不生成聊天 P/A、Core receipt、SourceAuthority 证明或人物画像，不调用模型，不自动抓取日志。总经验只保存操作者显式确认可共享的摘要，项目私密正文与文件路径不复制到全局。

## 迁移与配置

错题表与总经验表属于**同一可选域**，有两种安装方式：

- 全新数据库：`migrate` 在同一个已审查步骤里安装资料、错题与总经验对象。
- 已有 TS-080 资料库（`knowledge_schema=1` 且无 `lessons_schema`）：显式升级，必须先停写并备份。

```powershell
uv run python -m tianshu_memory.knowledge_cli --config C:/private/memory.json migrate-lessons --backup C:/private/before-lessons.sqlite
```

缺少 `lessons_schema` 时所有错题与总经验操作以 `dependency_unavailable` 失败关闭。迁移保留迁移前完整数据库；备份路径必须不存在，已升级的库再次迁移被拒绝。新增权威表 `lessons`、`lesson_history`、`experience_entries`、`experience_history`、`experience_lesson_refs` 全部带 `source_revision` 触发器；不重建或覆盖 schema 3 检查点，旧库恢复仍被 guard 拒绝。

权限沿用同一份 `knowledge.clients`，新增操作名与两个独立权限 `promote`、`review`：

```json
{
  "knowledge": {
    "projects": {
      "alpha": { "root": "C:/isolated/alpha", "host": "local", "default_branch": "main", "urls": [] },
      "beta":  { "root": "C:/isolated/beta",  "host": "local", "default_branch": "main", "urls": [] }
    },
    "clients": {
      "alpha-writer": {
        "credential_sha256": "<sha256>",
        "projects": ["alpha"],
        "permissions": ["import", "query", "lesson_record", "lesson_revise", "lesson_retire", "lesson_query", "lesson_recover", "lesson_check"]
      },
      "operator": {
        "credential_sha256": "<sha256>",
        "projects": ["alpha", "beta"],
        "permissions": ["query", "lesson_query", "lesson_recover", "lesson_check", "experience_promote", "experience_query", "experience_check", "experience_revoke", "experience_withdraw", "promote", "review"]
      }
    }
  }
}
```

`experience_promote` 需要操作名 `experience_promote` 加独立权限 `promote`；`experience_query`/`experience_check`/`experience_revoke`/`experience_withdraw` 需要各自操作名加独立权限 `review`。项目写权限、服务令牌、来源受理都不构成晋升批准。

## 项目错题

`lesson_record` 录入一条有版本的错题，`lesson_revise` 追加新版本，`lesson_retire` 废弃它：

```json
{"operation":"lesson_record","project_id":"alpha","arguments":{"key":"receipt-retry","expected_version":0,
 "lesson":{"trigger":"发送超时但未收到回执","symptom":"同一条消息被投递两次","cause":"重试前未读回执表",
  "correction":"先读回执表，跳过已确认工作","verification":"两项目隔离回归通过",
  "scope":{"platform":"windows","language":"python","framework":"stdlib",
   "applies_to":["幂等投递"],"excludes":["仅外发通知"]},
  "evidence":[{"block_id":"…","document_id":"…","version":1,"hash":"…"}]}}}
```

- `evidence` 必须是本项目 `query` 当前返回的块引用；跨项目块即使调用者能读两个项目也被拒绝（文档按项目解析）。没有证据（`evidence_required`）不能录入。
- `key` 是错题的稳定身份：`lesson_id = "lesson:" + fingerprint([project_id, key])`，版本从 1 开始。`lesson_revise` 必须复用同一个 `key`（否则 `identity_conflict`），并要求 `expected_version` 等于当前版本；`lesson_retire` 同样要求 `expected_version`。
- 一个错题可被多次修订：`key` 表示身份，可选 `dedupe` 表示本次请求的幂等键。省略时用 `key` 作为幂等键（因此同一身份只能写一次），需要再次修订时给新的 `dedupe` 或新的身份键。
- 相同幂等键同负载重放返回历史结果，异负载 `idempotency_conflict`；版本竞争 `version_conflict`。废弃后不再召回（`already_retired`），历史版本仍保留在 `lesson_history`。
- 错题内容指纹绑定其来源块的**内容哈希**：重复导入相同字节不改变指纹；来源被修改或删除立即改变。读取时重新核对，过期条目整条不返回并计入 `omissions=["stale_source"]`。

## 目标查询与短恢复包

`lesson_query` 参数 `text`、`budget_bytes`（256–32768），只在本项目 FTS 索引上检索当前 `ready` 错题，返回整条错题与引用；预算不足整条遗漏（`omissions=["budget"]`），不截断正文。

`lesson_recover` 参数相同但预算 1024–32768，返回三层短包的当前两层：`goal`/`unfinished`（若存在 `write_state`）与本项目当前错题，附带 `revision`、`registration` 和 `seal`。包与服务端绑定：每次复用前必须 `lesson_check`，替换、删除、修订、废弃、项目状态更新、配置变化或内容篡改都会使旧包失效。

## 总经验晋升

`experience_promote` 是显式批准，不是自动沉淀：

```json
{"operation":"experience_promote","project_id":"alpha","arguments":{"key":"receipt-promotion","expected_version":0,
 "entry":{"title":"重试前先查回执","rule":"超时重发前必须读回执表。",
  "applicability":["幂等投递"],"excludes":["仅外发通知"],"counterexamples":["fire-and-forget 遥测"],
  "recheck_after":"2027-01-01",
  "evidence":[{"project_id":"alpha","lesson_id":"…","version":1,"hash":"…"},
              {"project_id":"beta","lesson_id":"…","version":1,"hash":"…"}]}}}
```

- 至少两个**不同项目**各贡献一条错题：同一项目重复日志、或同一 `(project, lesson)` 重复两次都不满足（`insufficient_evidence`）。两个证据都必须是当前 `ready`、内容指纹匹配的错题版本（`stale_evidence`）。
- 调用者必须对信封项目有相应权限，并同时有权访问 `entry.evidence` 中每个 `project_id`；否则在读取任何项目数据之前以 `forbidden` 失败关闭，不泄露该项目或错题是否存在。
- 批准时记录当前证据集合（`project_id`/`lesson_id`/`version`/`hash`）。再次批准同一 `key`（`expected_version` 为当前版本、`dedupe` 为新值）只能重复已验证的证据集合，否则 `evidence_changed`；这是复核/恢复通道。
- 全局只保存摘要字段与受保护的引用；`query`/`check` 默认把证据显示为 `"protected"`，只有调用者显式按某项目过滤（`project_id`）时才返回自己有权项目的引用。

## 失效与撤权

总经验的可用性在**读取时**推导，不做后台改写、不把正文复制到全局。判断复用与 `lessons`/`query` 相同的当前证据校验，并且**按来源种类**执行：`file` 证据在事务外重读并双次校验内容哈希；`url` 证据是最近一次显式导入的快照，以其当前已导入版本、状态与摘要加"该项目仍登记该 URL"为准。URL locator 从不交给文件 reader，读取经验也不会触发联网抓取。

最终提交段（serve）对**任何来源种类**都比较记录指纹：由当前哈希重建的 `lesson_hash` 必须等于错题引用里的 `hash`，否则 `stale_evidence`。该比较**不以"本阶段是否读过文件"为条件**——纯 URL 与混合来源同样必须通过；只有捕获段（capture）因文件尚未读取而暂缓这一项，其余检查照常执行。因此用全零或改一位的 `hash` 无法晋升，历史遗留的错误指纹条目在 `query`/`check` 中也不会报 `live`。

| 情形 | 效果 | 行为 |
| --- | --- | --- |
| 来源错题被修订或被废弃 | `superseded` | 不再返回，`check` 判定失效 |
| file 证据被修改/删除、文档被删除或标记不可用 | `stale_source` | 同上 |
| url 证据被刷新到新版本、被逻辑删除，或刷新读取失败 | `stale_source` | 同上 |
| 来源项目不再登记该 URL | `stale_source` | 该快照不再被承认 |
| 记录指纹与当前来源不符（含历史遗留错误条目） | `stale_evidence`（写入）/ `stale_source`（读取） | 拒绝晋升；已存在条目不再返回 |
| 来源项目主动撤出（`experience_withdraw`） | `withdrawn` | 同上；只有记录该错题的项目可撤出 |
| 调用者失去某个来源项目的权限 | `unavailable` | 条目不出现，也不在 `check` 中泄露存在性 |
| 项目登记期间被改动 | `unavailable` | 本次读取失败关闭，不返回缓存结论 |
| 显式撤销（`experience_revoke`） | `revoked` | 发布一条新版本并标记撤销；不再返回 |

有效性与检索可见性是两件事：

- **不可见**（调用者无权读取条目的全部来源项目）的条目在 SQL 阶段就被排除，**不是候选**：它既不出现在 `entries`，也不产生任何 `omissions`，更不会挤占合法候选的排名或预算。无权条目的关键词命中因此无法被观察。
- **可见但已失效**（调用者有权读取全部来源）的条目不计入 `entries`，并在 `omissions` 中说明真实原因（`stale_source`/`superseded`/`withdrawn`/`revoked`）。这是对调用者本就可见数据的说明，不构成泄露。

`experience_check` 参数 `entry_id` 与 `package`（含 `seal`/`version`/`effect`），返回 `valid`/`reason`/`effect`/`version`。seal 由服务端密钥与调用者凭据派生并按条目证据策略计算，客户端知道自己凭据也不能给篡改包重新签名；缓存复用前必须 check。check 与 query 使用同一套当前证据校验。

`experience_query` 参数 `text`、`budget_bytes`、`project_id`（可空）。只有调用者有权读取全部来源项目的条目参与排名与预算；`project_id` 过滤在有效性判断之前应用，用于减少暴露面而不是扩大权限。

## 三阶段事务边界

每个操作保持"捕获 → 外部读取 → 提交"三段，共享写锁绝不跨文件系统 I/O：

1. **捕获（事务内，无 I/O）**：读取授权、登记、项目修订、目标版本、幂等结果，并记录每个被引用的 **file** 证据的期望哈希（url 证据在此只确认快照版本与登记，不联网、不读文件）。
2. **外部（事务外）**：导入抓取来源；查询/恢复/检查/写回/总经验读取的每个 file 证据在这里读取并双次校验内容哈希。慢来源不占用 Memory 写锁。
3. **提交（短事务）**：重读授权与登记，原子复核项目修订、版本与幂等结果，只根据第 2 段结果判断证据是否当前；总经验的 `effect` 也在本段按来源种类推导。

文件在两段之间变化时证据一律判为过期，不会冒充当前；总经验条目也不会停留在 `live`。

## 命令与验证

CLI 与 MCP 入口与 TS-080 相同，新增 `migrate-lessons` 子命令：

```powershell
uv run python -m tianshu_memory.knowledge_cli --config C:/private/memory.json action --client alpha-writer --credential-env TIANSHU_PROJECT_SECRET C:/private/lesson.json
uv run --extra mcp python -m tianshu_memory.knowledge_cli --config C:/private/memory.json mcp --client operator --credential-env TIANSHU_PROJECT_SECRET
```

MCP 暴露 18 个工具（7 个资料工具 + 11 个错题/总经验工具），读写分离、逐次验证权限，默认未启用、不监听 HTTP、不修改任何客户端全局配置。

```powershell
uv run --extra mcp pytest tests/test_lessons.py tests/test_lessons_transport.py tests/test_lessons_concurrency.py -q --basetemp .runtime/tests-ts081-targeted
uv run --extra mcp pytest -q --basetemp .runtime/tests-ts081
```

专项与全产品实际结果见 `docs/handoffs/TS-081.md`。协议候选见 `docs/candidates/project-lessons/v2/`，仍未发布为根合同。
