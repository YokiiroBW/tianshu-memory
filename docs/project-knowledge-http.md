# 项目知识与研究笔记受限 HTTP 入口

本入口把已经集成的项目知识检索与研究笔记全生命周期，通过受限 HTTP 暴露给
未来平台服务端的同源连接器。它是产品内部的受限接口候选，**不是已经发布的跨产品合同**，也不代表
任何网页已经联通：平台侧的同源连接器、用户身份映射、页面位置与浏览器验收必须在 TS-090 释放
所有权后另行双方冻结。此轮不写网页、不加前端引擎，也不改平台代码。

覆盖范围 = 现有 `KnowledgeApplication.execute` 的既有十二个操作及 CONNECT-M 显式启用的四个只读操作，语义与之**逐字相同**；本入口只负责
传输与有界准入，不复制授权、版本、幂等或引用校验规则。其中 `document_list` / `document_read` 是
TS-086 新增的项目资料目录与完整语义块分页阅读，接口、权限、预算、游标与迁移边界见
`docs/project-knowledge-catalog.md`：它们同样是 `execute` 的操作，同样只读，并同样要求该身份在
`knowledge.clients` 里持有**同名显式 permission**（`query` 权限不隐含枚举或直接读取）。

## 启动

```powershell
uv run python -m tianshu_memory.knowledge_cli `
  --config C:/private/memory-knowledge.json `
  serve --client project-web-reader --port 18135
```

- `--client` 必填：本进程唯一的固定身份，来自私有配置 `knowledge.clients`。请求方**无法**通过
  body、query 或 header 改变它。
- `--port` 必填且无默认值：端口是操作者的决定。不借用聊天入口默认的 8130。默认绑 `127.0.0.1`；非 loopback 仅由既有 `server_runtime` 在显式 TLS 证书、私钥和 Host 白名单齐备时接受，见 `docs/deployment-runtime.md`。
- **凭据完全来自每个请求的 `Authorization: Bearer`**，与 `action`/`mcp` 把同一个值交给同一个
  `execute` 完全一致。本入口**不配置任何服务端明文秘密**：没有 `--credential-env`，不读环境变量，
  因此不存在“启动时的服务凭据”这套第二规则。摘要、项目集合与权限仍只由 `knowledge.clients` 判定。
- `18135` 只是文档显式示例；绑定前请确认空闲，不要在真实数据上随意启动服务。

启动失败关闭（拒绝启动，不进入“看起来活着”的状态）：

| 情况 | 输出 |
|---|---|
| `--config` 不存在/无法解析/不是严格 JSON | `{"status":"failed","code":"invalid_configuration"}`，退出码 1 |
| `--client` 未在 `knowledge.clients` 登记（或缺权限表） | `{"status":"failed","code":"unregistered_client"}`，退出码 1 |
| `--port` 缺失或不是 1–65535 的端口 | argparse 拒绝（无默认值）；`--port 0` 由入口拒绝为 `invalid_configuration` |

启动**不**检查项目是否已迁移、来源是否可用、数据库是否有数据：这些仍由每个操作自己判定，缺失时
按领域原有错误失败关闭。启动也**不**检查任何凭据——凭据是每个请求的事，一个未携带凭据的请求会得到
401，而不是让进程拒绝启动。

## 路由

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 仅报告入口存活 |
| `POST` | `/local/v1/project-knowledge/action` | 唯一业务入口 |

默认关闭 OpenAPI/docs/redoc（`/openapi.json`、`/docs`、`/redoc`、`/docs/oauth2-redirect` 均 404），
避免额外接口外露；未知路径与错误方法也返回统一失败体。

### `GET /health`

```json
{"state": "listening", "entrypoint": "project_knowledge_http", "projects": null}
```

`listening` 只表示**本入口在监听**，绝不表示项目已迁移或资料可读取；`projects` 恒为 `null`。响应不含
路径、项目名单、凭据摘要、迁移状态或计数。请勿把 200 解释为“知识库已就绪”。该路由不需要 Bearer。

### `POST /local/v1/project-knowledge/action`

请求头：

```
Authorization: Bearer <该固定身份对应的凭据>
Content-Type: application/json
Host: 127.0.0.1:<本进程端口>    (或 localhost:<本进程端口>)
```

请求体：与现有执行体**原样一致**，不新增、不重命名字段。

```json
{"operation": "note_query", "project_id": "alpha", "arguments": {"text": "receipt", "budget_bytes": 8192}}
```

严格十六操作 allowlist；新增四项仍需 `knowledge.clients.<固定client>.http_read_operations` 显式逐项启用，旧客户端默认 415 `unsupported`。领域原有同名 permission 与 `experience_query` 的独立 `review` 也必须同时满足：

| 家族 | 操作 |
|---|---|
| 项目知识只读 | `query`、`recover`、`check`、`document_list`、`document_read` |
| 研究笔记 | `note_record`、`note_revise`、`note_withdraw`、`note_query`、`note_recover`、`note_status`、`note_check` |
| 经验与交接（逐 client HTTP 额外授权） | `lesson_query`、`experience_query`、`continuation_recover`、`continuation_check` |

`document_list` / `document_read` 是只读分页操作，**不**改变本入口的任何传输规则：同样的准入、
同样的每请求 Bearer、同样的媒体类型与字节上限。它们的参数是封闭集合（分页三参数
`limit`/`budget_bytes`/`cursor` 必须齐全，`cursor` 首屏写 `null`），预算按整个响应序列化后的 UTF-8
字节判定，超出即 `budget_too_small` 或截断加 `next_cursor`——详见
`docs/project-knowledge-catalog.md`。

经此入口**不可用**：`import`、`delete`、`write_state`、`status`、`directory_scan`、`directory_apply`、
lesson/experience 的记录、修订、撤回、晋升与全局审查写操作。新增 continuation 仅观察服务端已登记的 checkout 别名，执行只读 Git 命令；不接受原始路径或浏览器指定仓库。没有 HTTP 导入、目录扫描/应用、经验晋升或迁移端点。

成功响应 200，body 就是现有 `execute` 的结果（与 `knowledge_cli action`、MCP 同名工具完全一致），
例如 `note_record` 返回 `{"status":"recorded","note_id":...,"version":1,"hash":...}`。所有响应（含错误）
带 `Cache-Control: no-store`、`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`。

### 请求/返回实例

```powershell
curl.exe -s -X POST http://127.0.0.1:18135/local/v1/project-knowledge/action `
  -H "Authorization: Bearer <knowledge.clients 中该身份对应的凭据>" `
  -H "Content-Type: application/json" `
  -d '{\"operation\":\"query\",\"project_id\":\"alpha\",\"arguments\":{\"text\":\"receipt\",\"budget_bytes\":8192}}'
```

```json
{
  "project_id": "alpha",
  "blocks": [{"reference": {"block_id": "document:...:1:000", "document_id": "document:...", "version": 1, "hash": "..."}, "text": "Alpha source: ...", "spans": [[1, 1]], "provenance": {...}}],
  "omissions": [],
  "retrieval": "lexical",
  "trust": "source_material_not_instructions"
}
```

研究笔记一次完整链（同一固定身份）：

```json
{"operation":"note_record","project_id":"alpha","arguments":{"key":"n1","dedupe":"n1","expected_version":0,"note":{...}}}
{"operation":"note_revise","project_id":"alpha","arguments":{"key":"n1","dedupe":"n1-revise","note_id":"note:...","expected_version":1,"note":{...}}}
{"operation":"note_recover","project_id":"alpha","arguments":{"text":"receipt","budget_bytes":16384}}
{"operation":"note_check","project_id":"alpha","arguments":{"package":{...上一步返回的包...}}}
{"operation":"note_withdraw","project_id":"alpha","arguments":{"key":"n1-withdraw","note_id":"note:...","expected_version":2,"reason":"superseded"}}
```

资料目录→完整阅读一次链（同一固定身份）：

```json
{"operation":"document_list","project_id":"alpha","arguments":{"limit":8,"budget_bytes":32768,"cursor":null}}
{"operation":"document_read","project_id":"alpha","arguments":{"document_id":"document:...","expected_version":1,"expected_hash":null,"limit":8,"budget_bytes":32768,"cursor":null}}
{"operation":"document_list","project_id":"alpha","arguments":{"limit":8,"budget_bytes":32768,"cursor":"<上一步返回的 next_cursor>"}}
```

游标**只能**由同一身份、同一项目、同一操作、同样的 `limit`/`budget_bytes` 继续使用；改任何一个都是
`invalid_cursor`，页间项目 revision 变化则是 `cursor_stale`（都要从第一页重新开始）。

`key` 是笔记的**稳定身份**，`dedupe` 是**该次调用的幂等键**：一次修订/撤回必须带自己的 `dedupe`，
否则会与创建它的那次写入同键冲突。这与 CLI/MCP 的行为完全相同。

## 错误与重试

失败响应固定为 `{"status":"failed","code":"<稳定错误码>"}`。**状态码说明"哪一层"拒绝了请求，
`code` 说明"发生了什么"**，两者都不是权限分类的线索：

| 状态 | 来源层 | 何时 | `code` |
|---|---|---|---|
| 400 | 传输层（authority） | Host 非 loopback/非本端口，或浏览器 `Origin`/`Sec-Fetch-*` | `invalid_host`、`browser_origin_refused` |
| 400 | 传输层（请求文本） | 请求体不是严格 JSON 对象、字段不符/多余、字段类型错、`project_id` 长度不合规；或请求方中途断开 | `invalid_input` |
| 401 | 传输层 | 缺失/畸形 `Authorization: Bearer`（没有可交给领域的凭据） | `unauthorized` |
| 408 | 传输层 | **读体阶段**或**执行等待阶段**各自超过 10 秒 | `request_timeout` |
| 413 | 传输层 | 实际到达字节超过 262144 | `request_too_large` |
| 415 | 传输层 | `Content-Type` 非 `application/json`、操作不在 allowlist，或新增操作未获该 client 的 HTTP 额外授权 | `unsupported` |
| 503 | 传输层 | 四个准入槽已满（无等待队列） | `overloaded` |
| 503 | 依赖 | 存储/迁移/文件不可用 | `dependency_unavailable` |
| 422 | **领域**（`execute` 抛出的一切） | 授权、项目、版本、幂等、引用、参数等任何领域拒绝 | 领域原码：`unauthorized`、`forbidden`、`project_unregistered`、`project_uninitialized`、`version_conflict`、`stale_evidence`、`evidence_required`、`idempotency_conflict`、`invalid_input` … |

**分层规则（不靠 code 猜层）**：传输层与领域层各自在**抛出点**封装，状态码只由"谁抛的"决定。
因此同一个 `code` 名可以在两层以不同状态出现，这是正确的而不是矛盾：

- 请求体形状不合规 → 传输层 **400** `invalid_input`；领域拒绝一个参数不合规的 `arguments` → **422**
  `invalid_input`。同码不同层，状态码把层说清楚。
- `request_too_large` 只由传输层产生（领域没有这个码）；`stale_evidence`、`version_conflict` 只由
  领域产生（传输层没有这些码），因此它们各自只出现一次。
- 凭据与固定身份的摘要不符 → **422** `unauthorized`（领域读懂了并拒绝了），与"根本没带 Bearer"的
  **401** 区分开。

领域拒绝统一 422 是为了不按状态码泄露"缺哪一类权限"或"哪条规则先触发"；调用方只应读 `code`。
未知路径是 404 `not_found`、错误方法是 405 `method_not_allowed`，同样带统一失败体。错误体不含异常
路径、SQL、凭据或原始正文。

**两个独立阶段，各自 10 秒**：

- **读体阶段**：从准入后开始读正文起计时，超时 408 `request_timeout`。此时**没有发起任何领域调用**，
  槽立即归还。
- **执行等待阶段**：从发起同步 `execute` 起计时，超时 408 `request_timeout`。此时**操作可能仍会完成并
  提交**——入口不声称取消、不回滚、不重试。

两段时限不叠加计算，也不存在"同步工作绝不长期占槽"这种说法：槽的归属由**那次同步调用是否真的返回**
决定，而不是由响应是否写出决定。

**重试语义**（与写操作有关，务必按此实现连接器）：

- 读操作幂等，可直接重试；`query`/`recover`/`note_query`/`note_recover` 的字节预算与省略项语义不变。
- 写操作（`note_record`/`note_revise`/`note_withdraw`）**本入口绝不自动重试**，也**绝不**对进行中的
  写入回答"已取消"。执行等待超时（408）或断连只描述传输层：领域调用可能仍在运行并最终提交。此时用
  **同一份完整请求**（同 `key`、同 `dedupe`、同 `expected_version`、同内容）重放，得到已记录结果
  （`replayed: true`），或用 `note_status` 读回状态；不要发明新 `dedupe`、不要改写内容后复用旧键。
- `version_conflict`/`stale_evidence`/`idempotency_conflict` 都是**明确冲突**，禁止用自动重试掩盖：必须
  先重新读取当前版本或证据，再提交新的完整请求。

## 传输边界（固定值）

| 项 | 值 | 说明 |
|---|---|---|
| 请求体上限 | 262144 字节 | 按**到达字节流式累计**判定，不信 `Content-Length`；超限 413 |
| 准入 | 最多 4 个，**在读正文之前认领** | 满载立即 503 `overloaded`，**不读正文、不排队**；认领与检查是同一个操作，慢体数量同样受 4 个约束 |
| 准入槽覆盖范围 | 读正文 → 同步 `execute` 真实结束 | 槽只在"读失败/未发起调用"或"那次调用真的返回"时归还；响应写出（408、断连）不归还 |
| 读体时限 | 10 秒 | 超时 408 `request_timeout`，槽立即归还 |
| 执行等待时限 | 10 秒 | 超时 408 `request_timeout`，槽保持到调用真正结束 |
| Host 白名单 | 默认 `127.0.0.1`/`localhost` + 本进程端口 | 非 loopback 仅接受启动时显式验证的 TLS/Host authority；不接受任意 Host/DNS rebinding |
| 浏览器 | 一律拒绝 | 见 `Origin`/`Sec-Fetch-*` 即 400；无 CORS、无 cookie、不读 `X-Forwarded-*` |
| 缓存 | 无 | 全部响应 `no-store`；不建 HTTP 全局结果缓存 |

**为什么准入必须在读体之前**：若先读完正文再认领槽，四个槽之外的请求仍会各自把正文读进内存——那是
第二条无界队列，慢体可以无限堆积。本入口的做法是"读体前检查**并**认领"，两者是同一个不可分割的
操作，因此不存在"检查通过、认领之前"被穿透的窗口：第 5 个请求**一个字节都不会被读**，`execute` 也
不会被调用。

"槽持续到同步执行真正结束"同样是硬要求：否则一次超时就能让进程同时跑超过四个写事务。相应地，超时
后的重放由领域幂等账本回答，入口不重试、不取消。**读体失败、断连、超时都不会重复归还槽**：每条路径
只有一次归还。

## 身份与授权边界

- 身份 = 启动参数固定的一个 `knowledge.clients` 条目，绑定**本机受信操作者**。凭据摘要、项目集合与
  逐操作权限全部由现有私有配置决定，本入口不新增 token 数据库、账号映射、Platform resolve 假适配器，
  也不复制授权判断。
- 凭据 = 每个请求 `Authorization: Bearer` 携带的原值，直接交给同一个 `execute` 与
  `knowledge.clients` 比对。入口自身不持有、不缓存、不读取任何服务端秘密，也没有环境变量入口。
- 每个请求**新建** `KnowledgeApplication(config_path)` 并在线程池执行，只调用公共 `execute`；不共享
  可变请求域上下文，不调用私有领域方法（领域实例会改写自身 project/client/权限/域标志，跨请求复用
  会让上一请求的授权项目泄漏到下一请求）。
- 授权在每个写操作提交前由领域重新读取：撤销凭据或权限后，后续请求（含幂等重放）都被拒绝，且是
  **422 + 领域原码**，不是 401。
- **此凭据不是网页登录用户级授权**，绝不能下发到浏览器；本入口拒绝浏览器 Origin 请求，未来网页只能
  经平台服务端连接器访问。

## 与既有入口的关系

| 入口 | 身份来源 | 凭据 | 运行方式 |
|---|---|---|---|
| `knowledge_cli action` | `--client` | `--credential-env`（环境变量） | 单次进程，读一份请求文件 |
| `knowledge_cli mcp` | `--client` | `--credential-env`（环境变量） | stdio，官方 SDK |
| `knowledge_cli serve` | `--client` | 每请求 `Authorization: Bearer` | 默认 loopback；非 loopback 必须 TLS/Host 白名单的长驻进程 |

三者都是用同一个 `KnowledgeApplication.execute` 的适配器，规则只有一份；前两者的 `--credential-env`
保持不变，本入口不新增第二套凭据规则，也不改它们。

## 验收与专项命令

```powershell
uv run pytest tests/test_knowledge_http.py tests/test_knowledge_http_process.py -q --basetemp .runtime/tests-ts085-http
uv run pytest tests/test_knowledge_catalog_http.py -q --basetemp .runtime/tests-ts086-catalog-http
```

`test_knowledge_http_process.py` 会真实启动/停止/重启 `serve` 子进程，经真实 socket 完成
检索→记录→修订→重放→查询/核验→撤回→重启后状态保持，并与 `action` CLI 的结果逐字比对；并且：

- 子进程环境里**没有任何知识凭据**，用它验证"正确 Bearer 正常 / 错误 Bearer 领域 422 / 缺 Bearer
  传输 401"三种判定。
- 一个用例用真实 socket 打开四条请求、只写半个正文并停住，再用第 5 条**不带正文**的请求验证它立即
  得到 503 `overloaded`；随后补齐四个正文，四条都正常返回。
- 另一个用例在正文中途直接断开连接，验证入口仍然存活、仍然服务。

`test_knowledge_http.py` 用手写 ASGI 客户端（自己决定正文何时到达、并统计应用向服务器索取正文的
次数）验证准入先于读体：满载时第 5 条请求 `receive` 次数为 0、`execute` 调用次数不变。另有四个慢体与
四个慢执行两种饱和、读体超时、断连、调用抛错、以及调用真正返回时释放槽的确定性用例。其中一个用例
按**精确集合**断言这份 allowlist 是既有十二项加四项 opt-in 读，并让 `document_list` / `document_read` 经同一入口读到
一次真实导入留下的块。

`test_knowledge_catalog_http.py` 覆盖两个目录操作的入口层：真实 `response.content` 字节数与
`budget_bytes` 的比较、真实状态码与媒体类型、跨项目与伪造身份、游标绑定（跨操作、改 `limit`、改一个
字节）、页间 revision 变化后的 `cursor_stale`、来源被改写后不再返回旧正文、未升级库上的
`dependency_unavailable` 与同一库里旧操作仍可用。它的最后一个用例**真实启动** `serve` 子进程并经真实
socket 走"列表 → 阅读列表指名的文档 → 下一页"，然后把同样三个请求经进程内入口在同一数据库上再打一遍
并逐字比对——**只有这一个用例声称真实 socket**。

边界与线上行为已经用真实进程核对过的几条，写在这里以免误读：

- 未迁移/无笔记的项目：`note_query` 等读操作按领域原码返回 422 `project_uninitialized`，**不是** 500，
  也不是"空结果"。入口不猜测项目状态。
- 未迁移目录索引（`knowledge_catalog_schema` 缺失或索引形状不对）的库：`document_list` /
  `document_read` 返回 **503** `dependency_unavailable`，而同一个库上的 `query` 等旧操作照常可用。
- 请求体字段多一个（例如试图自带 `client`）在**到达领域之前**就被拒：**400** `invalid_input`。身份只能
  由进程启动参数决定；同一个码若来自领域拒绝则是 422，层不同、状态不同。
- 一个项目之外的 `document_id` 在 `document_read` 上是 **422** `stale_evidence`（"这份资料不是本项目
  证据"），不是 `forbidden`：拒绝本身不报告该文档是否存在于别处。
