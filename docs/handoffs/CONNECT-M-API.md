# CONNECT-M 浏览只读接口（产品候选，待协调发布）

本接口供 Platform 服务端调用，浏览器不持有 Memory Bearer、Platform origin 或 Memory 数据库连接。现有 `select`/`profiles/select` 保持上下文检索语义；下列端点读取实际有效组/共享投影，供目录和分页使用。接口先行稿；实现与隔离验证见 `CONNECT-M.md`。

## 部署身份

在 Memory 私有运行配置中为 Platform 分配独立 `callers.<name>`，仅授予 `browse`，配置 `token`、`issuer: "platform"`、固定 HTTPS `issuer_url`/`issuer_token`、`allowed_actors`。另登记 `browser_readers.<name>`：

```json
{
  "account": {"namespace": "web", "immutable_account_id": "稳定账号 ID"},
  "actor_id": "actor:已登记值",
  "scopes": [
    {"actor_id": "actor:已登记值", "person_id": "Memory 已登记 person ID", "audience": "self_private", "conversation_id": "已登记会话 ID"}
  ]
}
```

群范围另加完整 `audience: "group"` scope。服务凭据只能代表该固定账号、actor 和精确 scope；不能使用 `local_users` 凭据、Companion 凭据或来源 token。Platform 为当前已登录用户取得新鲜的 `origin.assertion_ref`，Memory 每次通过 HTTPS issuer 重解析，并要求其账号、actor、完整 scope 与登记完全一致。`person_id`/`actor_id`/`scope` 即使来自网页也只能匹配登记值，不能扩大范围。服务端配置移除/禁用 bearer 后下次请求拒绝；issuer 撤销或来源同步失败时拒绝，不读取旧缓存。

## 请求

三个端点均为 POST JSON，`Content-Type: application/json`，`Authorization: Bearer <Platform 专用服务凭据>`：

- `/internal/v1/memory/browser/overview`
- `/internal/v1/memory/browser/subjects`
- `/internal/v1/memory/browser/records`

公共字段：`schema_version: 1`、`request_id`（1–128 字符）、`origin: {"assertion_ref": "当次 Platform 签发引用"}`、`scope`（与配置和 origin 完全相等）。`subjects`/`records` 可加 `limit`（1–50，默认 20）、`cursor`（首面为 null/省略）；`records` 可加 `subject`，其格式为 `{kind:"person",person_id:"…"}` 或 `{kind:"group",conversation_id:"…"}`。不带 `subject` 读取该 scope 的本人记忆；带 `subject` 读取当前可见的共享画像，不能读取目标的私有记忆。`overview` 只接受公共字段。

成功均返回 `schema_version`、回显 `request_id`、`scope`、`verified_at`、`scope_version`；`overview` 另返回 `memory_group_count`、`counts_truncated`。它只统计本 scope 实际有效的本人记忆，最多检查 1000 个候选组；`counts_truncated=true` 时该数是下界，页面应显示“至少 N”。人物数量从 `subjects` 分页取得，不提供伪精确总数。`subjects` 返回 `items: [{subject, categories, group_count, group_count_truncated}]` 和 `next_cursor`；群主体限于当前群，计数上限 200，截断时同为下界。`records` 返回 `items: [{semantic_group_id, category, field_key, item_key, units:[{record_id, record_version, statement, conditions, negations, valid_time, uncertainty, reality}]}]` 和 `next_cursor`。只返回完整有效语义组；不返回私有来源、原文路径、审批凭据或内部 token。响应 `Cache-Control: no-store`，总 JSON 大小不超过 256 KiB。

分页是按稳定 subject/group key 的 HMAC 游标；若同一视图的 scope/来源版本在两页间变化，返回 `scope_changed`，客户端从首页重取。每次请求仍独立执行身份、权限与 SourceAuthority 屏障。每页最多 50 项、扫描最多 200 个候选；若一页扫描上限内全为失效项，可能返回空 `items` 与非空 `next_cursor`，继续下一页才算目录结束。空集合且 `next_cursor: null` 不表示断连。错误为 `{"status":"failed","code":"...","request_id":"..."}`，稳定码：`unauthorized` 401、`forbidden` 403、`invalid_input` 400、`scope_changed` 409、`queue_full` 429、`timeout` 408、`response_too_large` 413、`dependency_unavailable`/`log_unavailable` 503。无内部异常详情。

冻结的 source-sync/v1 `current_access` 只接受 Companion viewer。本浏览端点不伪造该身份：先执行 SourceAuthority 的无 viewer owner snapshot/当前 grant/final head 屏障，来源负面状态独立提交；屏障后在打开本地业务事务**之前**，重新认证专用 Bearer、通过真实 Platform HTTPS issuer 重解析同一 origin 并重读 `browser_readers`；最后在事务内比对来源 revision、本地 binding 和完整 scope 后读取。若同步期间凭据、origin、scope 或来源撤销，拒绝返回条目；这个组合方案需协调和独立审查后才能发布，不能称为冻结合同原 viewer 联合核验等价。

## 写操作与接入边界

本轮不提供网页批准/遗忘按钮：当前 `LocalUserApplication` 只接受用户审阅完整操作、独立用户凭据、实时 Platform resolve、绑定版本/精确权限和确认消费。Platform 如要新增网页写流程，先设计真正的用户确认和认证转交，协调审查后再接；服务 Bearer 或本浏览端点绝不能自动批准。账号关联仍使用已有 `identity/register`/`resolve`；`identity/link` 当前不可用，不能显示为可操作。

部署复用既有 Memory `serve` 的 TLS、Host、诊断与守护配置；没有默认公网地址或端口。所需运行配置和 Platform issuer route 由总控在受控部署中发放；本任务不操作 NAS 或真实数据。
