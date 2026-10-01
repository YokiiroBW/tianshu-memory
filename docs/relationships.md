# 角色—人物关系与好感（TS-114）

2026-10-01：Memory 隔离本地实现。根 `role-relationship/candidate-v1` 仍是候选，本文没有发布跨产品合同、安装生产表或授予调用权限。

Memory 是 `(actor_id, person_id)` 分数、关系类型、冻结区间和事件账本的唯一写者。Companion 读取投影和提交可信轮次候选；Platform 转发获准后台操作。关系类型不授予管理员、工具或机器人回复权限，不随阶段自动变成恋人。短期情绪仍属于 Companion。

## 装配与前置条件

领域代码位于 `src/tianshu_memory/relationships/`。`app.create_app` 注册内部端口；`configured_app` 从可选 `relationships_policy` 构造策略。`store.py` 只提供显式迁移挂钩，`workflow.py` 只接入旧增量适配器。启动服务不运行迁移。

生产装配沿既有 `source_sync` 模式、`Authenticator`、Platform issuer、SourceAuthority 与 Store checkpoint。调用方必须显式配置对应 operation；配置没有默认新增 grant。后台管理还必须有 `role_admin=true`、实时 Platform origin 内非空真实 `principal_id` 和当前 actor 授权。服务 Bearer、浏览器正文、昵称及模型声明均不能代替操作者身份。

只在已审阅的合成 schema 3 库、停止其他写者后显式调用：

```python
from tianshu_memory.relationships import Policy

report = store.migrate_relationships(
    synthetic_backup_path, clock=service.clock, policy=Policy()
)
```

迁移保存完整 SQLite 与匹配的 `.source-guard.json`，独占创建备份文件，安装六张受 source-revision trigger 跟踪的附加表，metadata `relationships_schema=1`。重复安装不重复导入。没有新增生产迁移 CLI、默认库路径或在线降级。失败不安装部分表；离线恢复必须同时使用同次 DB 和 checkpoint，不能只覆盖其中一个。

## 内部端口

四端口均为 `POST /internal/v1/relationships/<operation>`，JSON 请求上限 16 KiB；复用现有有界 Requests、先鉴权后读体、独立正文和执行超时、持久诊断与 no-store 响应。只接受服务认证；Origin/Cookie 请求拒绝，浏览器会话由 Platform 承担。正文不能提供 operator。

公共请求信封：

```json
{
  "schema_version": 1,
  "request_id": "synthetic-request:1",
  "origin": {"assertion_ref": "synthetic-platform-origin"}
}
```

| operation / permission | 信封内业务字段 | 返回字段 |
| --- | --- | --- |
| read / `relationships.read` | `pair`；可选 `managed=false` | `projection` |
| check / `relationships.check` | `pair`, `expected_version`；可选 `managed=false` | `check: {version, current: true}` |
| manage / `relationships.manage` | `command`；内层 request_id 必须等于信封 | `projection` |
| settle / `relationships.settle` | `candidate` | `settlement` |

正常响应信封为 `{schema_version: 1, request_id, <返回字段>}`；错误沿用 `Fault.wire`，不泄露上游内部错误。`pair` 恰为 `{actor_id, person_id}`。普通 read/check 必须等于实时 origin 的精确 pair。`managed=true` 是明确后台私密查询，还需上述管理权限及操作者，可查看同一获准 actor 下其他真实人物；它不能用于普通群聊上下文。

所有 pair、request_id、event_id、turn_id、source_ref 使用候选标识符规则 `^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$`；空格、中文、前导下划线和斜杠均拒绝。断连或执行超时不证明写入没有发生；调用方须保留相同人工 request_id 或候选 event_id，不得生成新ID重放。历史回执只证明一次操作的结果。

## 候选合同逐项核对

本地测试从根候选目录读取 schema，没有在产品内复制。2026-10-01 对照结果：

| 候选定义 | Memory 实现 | 实测依据与边界 |
| --- | --- | --- |
| RelationshipCommand | manage 的内层 command；三种操作字段、CAS、类型/长度/范围一致 | schema 验证正常样例；未知字段、布尔伪整数和非法标识符拒绝 |
| AffinityEventCandidate | settle 的内层 candidate；恰好七字段，无 delta | schema 验证候选；成功轮次、来源、时间与行为核实由应用层加严 |
| PrivateProjection | 私聊 read 与获准后台 manage/read | pair、版本、策略、类型、标签、分数、阶段、冻结、UTC时间字段一致 |
| PublicProjection | 普通群 read | 固定公开表达提示；schema 不含私密分数/阶段/冻结字段 |
| SettlementResult | 成功处理候选后的结算回执 | 接受、冻结、预算或无变化回执通过 schema；鉴权/来源错误仍是 HTTP Fault |

候选允许 `rejected_source/rejected_authorization/conflict` 枚举，不要求所有拒绝都返回200回执；本实现对这些请求拒绝并返回403/409。外层 HTTP 信封、`managed` 和 `check` 结果没有包含在这五类候选定义中，属于本地端口约定，正式发布需另补生产者/消费者共同确认。根候选 schema SHA256 为 `481a610729539005c42553c20336580acc9068b3729e43bdf575340698627bb1`，本轮未改根合同，也未宣称三产品共同验证。

`command` 使用候选 `RelationshipCommand`：`request_id, pair, expected_version, operation`，再按操作添加：

- `set_binding`：`relationship_type`（unspecified/friend/partner/family/custom），可选 `display_label`（最多40字符）。
- `set_freeze`：布尔 `frozen`。
- `adjust_affinity`：整数 `delta`（-100..100），非空 `reason`（最多200字符）。

人工操作以 `(真实principal, request_id)` 幂等并审计；同ID不同内容409。新操作以 Memory 当前 version 做 CAS；冲突由消费者重新读取后明确重试，不能静默覆盖。重复操作返回历史回执，不能把该回执当当前上下文。

`candidate` 恰为 `event_id, pair, kind, turn_id, source_ref, source_revision, occurred_at`，不接受 delta。来源引用使用 `MemoryService.source_identity(source, scope)`；同步模式是 admission key，不能把物理 message key 当成 admission key。

settle 验证已持久的真实、成功发送且有 reply_ids 的轮次，时间与 committed event 相同、含时区且不在未来；在当前来源 barrier 内再检查 turn/input_revision、scope_version、来源 revision/epoch、用户与角色授权。`conversation_completed` 默认 +1。`boundary_violation` 为 -4、`repair_acknowledged` 为 +1，但必须由同产品可信规则适配器 `behavior_verifier` 核实；默认未接适配器时返回 `unverified_behavior`，不信任模型提议。

## 消费者必须遵守的版本与隐私边界

私密 projection 有 pair/version/policy_version、关系类型、分数、阶段、冻结状态及UTC游标。读取会先刷新参与该 pair 的所有来源 scope，最多32个、owner head 漂移最多重试3次；来源不可用时拒绝返回缓存的私密分数。

普通群 projection 是固定低信息公开提示，不读取私密关系行或其 version；没有 score/stage/type/frozen。默认群事件不自动计分。TS-115 的 prepare/generate/send 应沿既有来源与身份链复核，再调用 check；版本冲突、撤权或依赖不可用时丢弃旧投影，不能凭历史 settle/manage 回执继续发送。TS-116 只做同源后台转发，不直接访问本库。

## 策略、冻结和纠正

默认范围 -1200..1200，八阶段起点为 -1200/-800/-400/0/200/600/900/1200，迟滞20，最高进入阈值截到最大分，因此1200可达。自动单次限 +4/-12，每 pair 每策略日正向总额12。默认 UTC；可配置 IANA 时区，环境须已有相应时区数据。策略在每个 pair 首次建立时固化，配置变化不自动改写旧 pair。

正分活动后宽限3个完整日；第4–7日2/天，第8–14日5/天，第15日后8/天趋于0。负分不自动向正分增长。衰减按注入时间计算，持久 clock_head 不回退，闭式计算支持长期离线。

冻结命令与结算使用同一 Store 事务。冻结停止自动正负变化和衰减；记录冻结区间，迟到但发生于冻结内的事件仍拒绝。解冻从当下重新开始宽限，不追补；重复解冻不重置游标。显式人工修正单独审计。来源失效、授权撤销、遗忘与纠错不被冻结挡住：失效事件标记后按保留的实际已应用增量重建分数，自然衰减最多扣剩余正分，避免“已衰减的 +1 被撤销后变 -1”。正向额度不会因来源失效返还。

## 旧增量接管

保留 `relationship_entries` 原始值和 provenance。可确定 actor/person、单一 scope、有效 lineage 且总值在范围内时一次性导入；失效来源留历史但不计分。多个旧 scope、超范围或映射不明记录为 pending，保留原值，不猜人物、合并私密范围或清零。pending pair 新投影返回 `migration_pending`；既有内部私聊兼容读取仍按旧精确 scope 求有效值。

新旧结算共用来源 revision/epoch 去重与预算，不累加两份分数。新旧同一来源只会贡献一次。旧 workflow 把已持久 committed event 的 occurred_at 带入冻结区间判断；成功写入旧原始增量不表示该增量被自动规则接受。

## 验证与交付边界

本轮最终定向71项已实际通过，含真实本地HTTPS的合成 Core/Platform owner、实际 Memory 鉴权和 SQLite。完整套件1074 passed、1个已在干净基线复现的400/403断言失败、6项因缺少mcp跳过。该测试没有运行真实 Core/Platform 产品、NAS、QQ或模型。准确失败/skip、基线格式问题见 `docs/handoffs/TS-114.md`。

后续需协调者审阅候选、固定交付版本并决定正式合同发布；再接 TS-115/116 和真实三产品联合。生产 alias/关系迁移、角色sidecar一致备份及NAS版本选择另行安排。当前产品main包含未部署QQ身份，不能直接当线上更新版本。


## TS-116 接线发现与修正（2026-10-01）

旧0b822425管理接口接受人工原因但仅保存请求摘要，原因无法回读；没有有界历史端口。现对应TS-114分支补齐：人工管理事件的既有result JSON附带操作/原因审计，返回PrivateProjection及命令重放保持原五DTO不变，不新增表/自动迁移。旧事件未记录原因时返回null，不编造。

新增POST /internal/v1/relationships/history，输入schema_version/request_id/origin/pair/managed=true，使用既有relationships.read服务能力，并要求role_admin、relationships.manage、有效Platform操作人和self_private origin；输出history={projection,items,has_more}。按该pair现有索引取最近20+1事件，只回日期、类型、实际增量、结果、有效性和人工原因，不回来源原文/凭据/操作人ID。历史和当前分数在同一来源屏障事务读取，失效来源先纠正。它是本地接口增量，候选五DTO/schema哈希不变，尚未根合同正式发布或部署。

原0b专项71通过；新增原因/边界测试5项及真实TLS端口1项须以本次记录为准，首次遗漏证书工具的5项setup错误不算通过。最终结果见后续提交交接与本轮检查点。

最终本轮验证：76项关系专项通过（9.72s），新增TLS历史端口用例随后在完整回归通过。完整Memory实际1080 passed / 1 failed / 6 skipped / 2 warnings，290.67s，.runtime/tests-ts114-history-full；唯一失败仍是此前已在干净主线复现的test_confirmation_rejects_mismatched_authority[account-403]（400与403旧断言差异），保留不扩修；6项缺MCP跳过保留。本次5个改动Python文件Ruff/format及AST、git diff --check通过。仅对应TS-114分支本地提交，不推送/合主线/部署。
