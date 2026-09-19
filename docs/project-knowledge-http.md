# 项目知识与研究笔记受限 HTTP 入口

本入口把已经集成的项目知识检索与研究笔记全生命周期，通过**仅 loopback** 的受限 HTTP 暴露给
未来平台服务端的同源连接器。它是产品内部的受限接口候选，**不是已经发布的跨产品合同**，也不代表
任何网页已经联通：平台侧的同源连接器、用户身份映射、页面位置与浏览器验收必须在 TS-090 释放
所有权后另行双方冻结。此轮不写网页、不加前端引擎，也不改平台代码。

覆盖范围 = 现有 `KnowledgeApplication.execute` 的十个操作，语义与之**逐字相同**；本入口只负责
传输与有界准入，不复制授权、版本、幂等或引用校验规则。

## 启动

```powershell
# 凭据只放环境变量：进程参数会出现在进程列表里，请求体永远不能指定身份。
$env:TIANSHU_PROJECT_CREDENTIAL = '<knowledge.clients 中该身份对应的凭据>'
uv run python -m tianshu_memory.knowledge_cli `
  --config C:/private/memory-knowledge.json `
  serve --client project-web-reader --credential-env TIANSHU_PROJECT_CREDENTIAL --port 18135
```

- `--client` 必填：本进程唯一的固定身份，来自私有配置 `knowledge.clients`。请求方**无法**通过
  body、query 或 header 改变它。
- `--credential-env` 必填：凭据所在环境变量名。启动时读取一次用于失败关闭，之后**每个请求重新读取**，
  因此轮换或清空该变量无需重启即生效。凭据值不打印、不记录、不返回。
- `--port` 必填且无默认值：端口是操作者的决定。不借用聊天入口默认的 8130，也不提供可公开绑定的
  host 选项——进程只绑 `127.0.0.1`。
- `18135` 只是文档显式示例；绑定前请确认空闲，不要在真实数据上随意启动服务。

启动失败关闭（拒绝启动，不进入“看起来活着”的状态）：

| 情况 | 输出 |
|---|---|
| `--config` 不存在/无法解析/不是严格 JSON | `{"status":"failed","code":"invalid_configuration"}`，退出码 1 |
| `--client` 未在 `knowledge.clients` 登记（或缺权限表） | `{"status":"failed","code":"unauthorized"}`，退出码 1 |
| `--credential-env` 未设置或短于 16 字符 | `{"status":"failed","code":"missing_service_credential"}`，退出码 1 |

启动**不**检查项目是否已迁移、来源是否可用、数据库是否有数据：这些仍由每个操作自己判定，缺失时
按领域原有错误失败关闭。

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

严格十操作 allowlist；其余操作即使该身份持有权限也不能经此入口调用（返回 415 `unsupported`）：

| 家族 | 操作 |
|---|---|
| 项目知识只读 | `query`、`recover`、`check` |
| 研究笔记 | `note_record`、`note_revise`、`note_withdraw`、`note_query`、`note_recover`、`note_status`、`note_check` |

经此入口**不可用**：`import`、`delete`、`write_state`、`status`、`directory_scan`、`directory_apply`、
`continuation_recover`、`continuation_check`、lesson/experience 系列。没有 HTTP 导入、目录扫描、
Git 观察、经验晋升或迁移端点。

成功响应 200，body 就是现有 `execute` 的结果（与 `knowledge_cli action`、MCP 同名工具完全一致），
例如 `note_record` 返回 `{"status":"recorded","note_id":...,"version":1,"hash":...}`。所有响应（含错误）
带 `Cache-Control: no-store`、`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`。

### 请求/返回实例

```powershell
curl.exe -s -X POST http://127.0.0.1:18135/local/v1/project-knowledge/action `
  -H "Authorization: Bearer $env:TIANSHU_PROJECT_CREDENTIAL" `
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

`key` 是笔记的**稳定身份**，`dedupe` 是**该次调用的幂等键**：一次修订/撤回必须带自己的 `dedupe`，
否则会与创建它的那次写入同键冲突。这与 CLI/MCP 的行为完全相同。

## 错误与重试

失败响应固定为 `{"status":"failed","code":"<稳定错误码>"}`。状态码只说明“怎么反应”，`code` 说明
“发生了什么”；**除下列前几行外的一切拒绝都是 422**，调用方不需要靠状态码区分“请求没读懂”和
“读懂了但拒绝”：

| 状态 | 何时 | `code` |
|---|---|---|
| 401 | 缺失/畸形 `Authorization: Bearer`，或凭据与固定身份的摘要不符 | `unauthorized` |
| 408 | 读体+执行超过时限（不声称取消） | `request_timeout` |
| 413 | 实际到达字节超过 262144 | `request_too_large` |
| 415 | `Content-Type` 非 `application/json`，或操作不在 allowlist | `unsupported` |
| 503 | 活动执行已满（无等待队列），或存储/迁移不可用 | `overloaded`、`dependency_unavailable` |
| 400 | Host 非 loopback/非本端口，或浏览器 Origin（这两类是 authority 判定，不属于 body 语义） | `invalid_host`、`browser_origin_refused` |
| 422 | **统一拒绝信封**：请求体不合规（非严格 JSON 对象、字段不符、操作名非字符串等），以及一切领域拒绝 | `invalid_input`、`forbidden`、`project_unregistered`、`project_uninitialized`、`version_conflict`、`stale_evidence`、`evidence_required`、`idempotency_conflict`、`project_conflict` … |

领域拒绝统一用 422 + 原码，是为了不按状态码泄露“缺哪一类权限”或“哪条规则先触发”；调用方只应读
`code`。未知路径是 404 `not_found`、错误方法是 405 `method_not_allowed`，同样带统一失败体。错误体
不含异常路径、SQL、凭据或原始正文。

**重试语义**（与写操作有关，务必按此实现连接器）：

- 读操作幂等，可直接重试；`query`/`recover`/`note_query`/`note_recover` 的字节预算与省略项语义不变。
- 写操作（`note_record`/`note_revise`/`note_withdraw`）**本入口绝不自动重试**，也**绝不**对进行中的
  写入回答“已取消”。超时（408）或断连只描述传输层：领域调用可能仍在运行并最终提交。此时用**同一份
  完整请求**（同 `key`、同 `dedupe`、同 `expected_version`、同内容）重放，得到已记录结果（`replayed: true`），
  或用 `note_status` 读回状态；不要发明新 `dedupe`、不要改写内容后复用旧键。
- `version_conflict`/`stale_evidence`/`idempotency_conflict` 都是**明确冲突**，禁止用自动重试掩盖：必须
  先重新读取当前版本或证据，再提交新的完整请求。

## 传输边界（固定值）

| 项 | 值 | 说明 |
|---|---|---|
| 请求体上限 | 262144 字节 | 按**到达字节流式累计**判定，不信 `Content-Length`；超限 413 |
| 读取+执行时限 | 10 秒 | 覆盖整段读体与执行；超时 408，不声称取消 |
| 并发执行 | 最多 4 个 | 满载立即 503 `overloaded`，**无等待队列**；槽持续到同步执行真正结束 |
| Host 白名单 | `127.0.0.1`/`localhost` + 本进程端口 | 不接受其他 host、其他端口、任意 Host/DNS rebinding |
| 浏览器 | 一律拒绝 | 见 `Origin`/`Sec-Fetch-*` 即 400；无 CORS、无 cookie、不读 `X-Forwarded-*` |
| 缓存 | 无 | 全部响应 `no-store`；不建 HTTP 全局结果缓存 |

“槽持续到同步执行真正结束”是硬要求：响应写出（含 408 超时、断连）**不会**释放槽，槽由那次领域调用
自己结束时释放。否则一次超时就能让进程同时跑超过四个写事务。相应地，超时后的重放由领域幂等账本
回答，入口不重试、不取消。

## 身份与授权边界

- 身份 = 启动参数固定的一个 `knowledge.clients` 条目，绑定**本机受信操作者**。凭据摘要、项目集合与
  逐操作权限全部由现有私有配置决定，本入口不新增 token 数据库、账号映射、Platform resolve 假适配器，
  也不复制授权判断。
- 每个请求**新建** `KnowledgeApplication(config_path)` 并在线程池执行，只调用公共 `execute`；不共享
  可变请求域上下文，不调用私有领域方法（领域实例会改写自身 project/client/权限/域标志，跨请求复用
  会让上一请求的授权项目泄漏到下一请求）。
- 授权在每个写操作提交前由领域重新读取：撤销凭据或权限后，后续请求（含幂等重放）都被拒绝。
- **此凭据不是网页登录用户级授权**，绝不能下发到浏览器；本入口拒绝浏览器 Origin 请求，未来网页只能
  经平台服务端连接器访问。

## 与既有入口的关系

| 入口 | 身份来源 | 凭据 | 运行方式 |
|---|---|---|---|
| `knowledge_cli action` | `--client` | `--credential-env` | 单次进程，读一份请求文件 |
| `knowledge_cli mcp` | `--client` | `--credential-env` | stdio，官方 SDK |
| `knowledge_cli serve` | `--client` | `--credential-env` | loopback HTTP，长驻进程 |

三者都是用同一个 `KnowledgeApplication.execute` 的适配器，规则只有一份。

## 验收与专项命令

```powershell
uv run pytest tests/test_knowledge_http.py tests/test_knowledge_http_process.py -q --basetemp .runtime/tests-ts085-http
```

其中 `test_knowledge_http_process.py` 会真实启动/停止/重启 `serve` 子进程，经真实 socket 完成
检索→记录→修订→重放→查询/核验→撤回→重启后状态保持，并与 `action` CLI 的结果逐字比对；并发用例
用真实线程+真实 socket 打满四个执行槽，确认满载只会得到 503 而不会排队。

边界与线上行为已经用真实进程核对过的几条，写在这里以免误读：

- 未迁移/无笔记的项目：`note_query` 等读操作按领域原码返回 422 `project_uninitialized`，**不是** 500，
  也不是“空结果”。入口不猜测项目状态。
- 请求体字段多一个（例如试图自带 `client`）在**到达领域之前**就被拒：422 `invalid_input`。身份只能由
  进程启动参数决定。
