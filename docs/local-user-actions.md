# 本地用户确认与画像操作（TS-034）

本入口只用于同产品、受信本地应用或 CLI。执行完整操作文件即表示用户明确批准其中的操作；不会把模型提议、来源受理、平台身份 assertion 或服务 token 当作同意。网页认证与自动审批服务不在本版。源合同仍为 source-sync/text-dialogue/profile-memory 已发布 1.0.0，无新跨产品 HTTP 写接口。

## 配置与身份

在既有 `source_sync` 私有配置中增加 `local_users`。部署者先经正常 identity/register/resolve 取得稳定 person/binding，登记账号、actor 和精确范围，不按昵称登记。配置和数据库、备份、操作文件仅放被忽略的 `.runtime/`，并由操作系统权限保护；它们属于信任边界，不能让模型或不受信用户改写。运行配置的 `callers.companion` 继续使用真实 Platform HTTPS origins/resolve，服务令牌和用户凭据分离。

```json
{
  "local_users": {
    "local-owner": {
      "credential_sha256": "替换为独立随机凭据的UTF-8字节SHA256十六进制摘要",
      "account": {"namespace": "web", "immutable_account_id": "registered-stable-account"},
      "actors": ["actor:a"],
      "revision_scopes": [
        {"actor_id": "actor:a", "person_id": "registered-person", "audience": "self_private", "conversation_id": "registered-private-conversation"}
      ],
      "profile_permissions": [
        {"role": "owner", "actor_id": "actor:a", "subject_kind": "person", "category": "interest", "sharing": "public_preference", "conversation_id": null},
        {"role": "owner", "actor_id": "actor:a", "subject_kind": "person", "category": "interest", "sharing": "group_only", "conversation_id": "registered-group"},
        {"role": "curator", "actor_id": "actor:a", "subject_kind": "group", "category": "topic", "sharing": "group_only", "conversation_id": "registered-group"}
      ],
      "disabled": false
    }
  }
}
```

这是结构示例，不能原样运行。凭据应使用密码学随机生成的至少 32 字符独立 token，不用人类密码；本地 `credential_digest(token)` 计算摘要。用户运行环境单独提供 token，命令行只传环境变量名称，不在操作 JSON 内带 token。代码还拒绝把本配置中任何入站/出站服务 token 登记为用户凭据。操作主体每次执行及业务写事务内重读配置，核对 HTTPS resolve 的账号与 actor、Memory 当前账号绑定及完整 allowed_scope；不存在 local_users 为 503，凭据不符为 401，范围不符为 403。

`profile_permissions` 是可复用的部署权限登记，**不是自动批准内容**。每次 `approve_profile` 仍明确执行完整草稿；后续 `publish_profile` 消费确切批准。owner 只批准本人的 interest；person style、group topic/style 必须登记 curator 的精确 actor/主体类型/类别/当前群权限。群整理者可使用该群其他成员的来源，但不能以群整理权公开他人兴趣或引用私聊推断全群。本人可明确将自己的私密兴趣分享至当前获准群；所有 public_preference 都只能本人 interest。无通配符、脚本策略、任意 True 回调或自动把 group_only 升级为 public 的路径。

## 显式迁移

确认功能使用既有 schema 3 confirmations，不需要额外表。画像批准/发布/撤销先停掉本库全部写入者，执行一次加法迁移：

```powershell
uv run tianshu-memory --config .runtime/local/config.json migrate-users --backup .runtime/local/backups/before-users.sqlite
```

前置为已完成 migrate-profiles、migrate-sources 的 schema 3。迁移新增 `profile_approval_authorities` 和 `local_users_schema=1` 特征标记；旧 schema 3、wire 与 manifest 不变。旧 fixture 批准没有真实主体记录，不能借迁移成为可信批准。新表 insert/update/delete 都推进 source_revision，沿现有 source-guard 提交。没有该特征时画像操作为 503。

备份使用 SQLite backup API 获取含 WAL 的已提交快照，必须是不同于数据库和 guard 的新路径，不覆盖。迁移事务失败回滚表与特征标记；保留备份。若 guard 已写、数据库 commit 失败，继续失败关闭，不自动重建 guard。停止全部写入，保留最新数据库/WAL/guard/备份并按 [来源恢复说明](source-sync-runtime.md) 审查；不能直接把迁移前旧库恢复并覆盖 guard。成功迁移后可暂停用户入口，不能删除新增表、降级数据库或丢弃已消费/撤销账本。代码回滚只允许保留最新数据库及 guard；旧版本不能承担新增用户入口，恢复批准/解锁不在此任务。

## 操作文件与运行

```powershell
# TIANSHU_LOCAL_OWNER_CREDENTIAL 由本机私有环境提供，不能提交或写进命令历史。
uv run tianshu-memory --config .runtime/local/config.json user-action .runtime/local/operation.json --principal local-owner --credential-env TIANSHU_LOCAL_OWNER_CREDENTIAL
```

CLI 只使用已有固定配置；无 token 命令行参数。一次读取整个 JSON，拒绝额外顶层字段、重复键、NaN 和超过 256 KiB 的文件。`verified_context`、`binding_version` 和 confirmed 不接受用户自报。来源内容、单位限定和完整草稿应由用户在执行前审阅；批准入口本身不调用模型生成内容。

遗忘/更正文件：

```json
{
  "operation": "confirm_revision",
  "request": "替换为正式 identity-memory#revise_request 完整对象",
  "expires_at": "2030-01-01T00:00:00Z"
}
```

request 包括 command（有效 origin、request_id、idempotency_key、deadline_at）、record_id、expected_version、revision_kind、confirmation_ref、evidence_refs、replacement_statement。这里只登记确认，输出正式 confirmation_record；随后由既有受信调用者将**同一 request**提交现有 `/internal/v1/memory/revise`。摘要按合同的 semantic_request，绑定账号/完整 scope/binding/record/expected_version/期限。HTTP 同幂等重试仍可取得原结果；新幂等键不能重用已消费确认。SourceAuthority 的远端失效先独立提交，再进行确认或修订业务事务。correct 仅禁旧值并返回 pending/invalidated，不把 replacement 伪装成新可读记忆。

画像文件：

```json
{
  "operation": "approve_profile",
  "origin": {"assertion_ref": "current-platform-origin"},
  "draft": "替换为下述完整草稿对象",
  "expires_at": "2030-01-01T00:00:00Z"
}
```

草稿字段严格为 source_scope、subject、sharing、conversation_id、category、field_key、units。subject 为 person+person_id 或 group+conversation_id；每个 unit 完整包含 statement、conditions、negations、valid_time、uncertainty、reality、sources。共享前执行原有 `profiles.validate_draft`，不截短单位限定；发布输出只有 shareable_projection，不能泄露原始来源引用。

approve 输出批准 ref。publish 文件沿用完全相同的 draft 和当前 origin，将 operation 改为 `publish_profile`，去掉 expires_at，增加 `approval_ref`。revoke 同样使用完整 draft/origin/approval_ref，operation 为 `revoke_profile`。批准/发布验证完整来源 snapshot、账号绑定与部署权限登记摘要；任何变更须重新批准。发布同 ref 幂等但不恢复已失效组；撤销对待发布或已发布批准都持久生效，已发布投影失效及共享 epoch 同事务推进。撤销仍需当前认证/权限和来源屏障可用；远端不可用时失败关闭，不能宣称撤销成功。修改部署权限/禁用主体会拒绝后续操作，已发布投影撤销使用明确 revoke 操作；权限配置不是投影自动回收任务。

同产品应用可调用 `LocalUserApplication(service, config_path).execute(action, principal=..., credential=...)`，service 必须是此部署配置组成的真实 SourceAuthority 服务。只在已获用户明确操作的应用流程中调用；不向模型注册为可自主调用的同意工具。不使用 LocalWorkflow 或自定义返回 True 的适配器。

## 验证边界

新增回归使用真实 CLI 子进程、独立凭据/配置校验、实际 Memory HTTP/SQLite/SourceAuthority 和明确合成的 HTTPS Core/Platform owner；没有操作真实用户、聊天或生产数据。原工作流局部测试保留为隔离业务登记检查，不能算新入口证据。TS-050 基线的 503/403 历史结果不变，待协调者集成后另跑新三方正向链路。完整 L0 未由本任务证明。

```powershell
$env:TIANSHU_TEST_CERT_PYTHON = 'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
uv run pytest tests/test_user_actions.py tests/test_trusted_workflow.py -q --basetemp .runtime/tests-ts034-users
uv run pytest -q --basetemp .runtime/tests-ts034-full
```
