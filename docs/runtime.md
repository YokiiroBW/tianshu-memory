# 本地运行与接入

安装、静态检查和产品测试命令以根 README/AGENTS 为准。

## HTTP

| 路径（POST） | 输入 → 输出 |
| --- | --- |
| /internal/v1/identity/resolve | resolve_request → resolve_response |
| /internal/v1/identity/register | register_request → identity_response |
| /internal/v1/identity/link | link_request → 当前 503，证明流程未实现 |
| /internal/v1/memory/select | select_request → select_response |
| /internal/v1/memory/revise | revise_request → revise_response |
| /internal/v1/memory/turn-commits | conversation.committed_event → consume_receipt |
| /internal/v1/memory/profiles/select | profile-memory/v1 profiles.select_request → select_response（显式启用） |

所有内部接口使用 `Authorization: Bearer <每调用服务独立令牌>`。请求格式来自 1.0.0。错误是 common.error，不包含私有记录存在性或请求正文。限制请求体 262144 字节。未配置启动可用 `uv run uvicorn tianshu_memory.app:configured_app --factory --host 127.0.0.1 --port 8130`；/health 与业务均返回 503，无默认成功。

select 任一预算维度为零时，只核验当前账号/范围元数据和 known_scope_version，不读正文、不建立 FTS。来源会话尚未绑定为 503、明确错配为 403、旧 scope_version 为 409。field:/item: 精确查询在 SQL 内先缩小组范围并跳过 FTS。对已 tombstoned 的记录提交 correct 为 invalid_input/400；v1 无恢复操作，新的证据或确认不能改变这条规则，原幂等 forget 仍可重放。

运行配置由 TIANSHU_MEMORY_CONFIG 或 CLI --config 显式指向，属于服务端私有文件。支持字段：

- `database_path`：明确本地 SQLite 文件；不支持 PostgreSQL DSN 或网络共享。
- `contract_directory`：已发布 contracts/text-dialogue/v1，启动核验固定 manifest SHA256 与全部文件摘要。
- `mode`：仅 `local_fixture` 提供合成来源 ledger；其他模式来源核验不可用。
- `callers.<service>.token/operations/allowed_actors`：该服务的凭据、允许操作及角色，操作名为 resolve/register/link/select/revise/consume。新画像权限独立使用 select_profiles。
- `callers.<service>.issuer`：nonebot 或 platform；正式来源解析可配置 issuer_url（完整固定 HTTPS 端点）与 issuer_token。未配置拒绝。
- `callers.companion.event_scopes`：仅事件消费使用的精确 scope 列表，从服务器授权配置读取。事件不使用 origins，不得把请求自报 scope 自动加入此列表。
- `origins`：仅 local_fixture 模式使用的合成来源引用 → trusted_context；每次从文件重读，测试撤销/过期生效。正式模式不接受此替身。

运行 CLI 固定 loopback，不提供公网或 TLS 部署命令。生产仍须完成凭据签发/撤销、TLS、來源适配与数据库方案，不能把本地 Bearer 演示外露作为生产。

## 候选审核应用层

本项目 LocalWorkflow 只允许 local_fixture 后端，用于可执行的隔离来源/审核步骤。JSON 输入格式是 `{ "operation": "...", "arguments": { ... } }`，通过 `uv run tianshu-memory --config <config> fixture-action <json>` 调用：

| operation | arguments |
| --- | --- |
| observe_source | source（common.source）、scope、reality（real/fictional）、state（active/withdrawn） |
| commit_candidate | job_id、drafts（完整语义组数组，或 []） |
| confirm_revision | request（整个 revise_request）、account、scope、expires_at |
| acknowledge | event_id（内部 outbox 投递得到实际确认后） |
| approve_profile | draft（完整目标/范围/字段/语义/来源）、context（合成 issuer）、expires_at |
| publish_profile | draft、approval_ref（上一步返回）、context（当前合成 issuer） |

draft 每组包含 scope、category（identity/style/relationship/current_items/evidence）、可选 field_key/item_key、units。每个 unit 必須包含 statement、conditions、negations、valid_time、uncertainty、reality、sources。sources 必须是该 job 已核验原文引用子集，不接受客户端 projection_ref；群投影由服务自行签发。relationship 类组可附整数 relationship_delta（-100..100），按来源版本账本累计。

`jobs` 查看持久工作状态，`rebuild-index` 重建当前合法条目，`outbox` 读取最多 100 个未确认事件。读取不会删除事件，实际确认后才能 acknowledge，重启继续投递。无生产 worker 自动运行，不会背景调用模型或发消息。

外部原文服务并无本项目自造读取端点。来源核验和原文回读的生产 wire 若要增加，应先由主协调者发布兼容合同，当前接入方需保留不可用状态。

TS-031 的显式迁移、共享批准和独立版本域详见 [共享画像运行边界](profile-runtime.md)。
新增 fixture-action 仍只接受受信本地合成操作，不能将其 context 当生产用户自报凭据。
任一 caller 显式获准 select_profiles 时，从 contract_directory 所在 contracts 根目录加载
profile-memory/v1，验证已发布固定摘要后注册新路由。否则仅注册原 v1 路由，无需新包。
