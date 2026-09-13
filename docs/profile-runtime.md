# TS-031 共享画像运行边界

绑定主协调 `688b0ce` 发布的 profile-memory/v1 1.0.0；manifest LF SHA256
`488d05438dd5b5abaa43a66a7eab0eb5cf615d5af01a964a7286cd23e68f7eb7`。
原 text-dialogue/v1 1.0.0 摘要仍为
`81e6cc4ddef7c6f82e055d4cb04b090db036dd5c52763473ce697aa02db478a1`。

在现有 callers 的 operations 中显式添加 `select_profiles`，服务才加载同 contracts 根目录
下的 profile-memory/v1 并注册 POST `/internal/v1/memory/profiles/select`。
无新配置键或依赖。新路由仍需源身份认证和 schema 2；无该操作权限403，未迁移503。
旧配置不加载画像包，原 v1 继续运行。健康信息会显示已加载的 profile_contract_version。

当前产品验证为本地 SQLite、合成来源及明确合成批准。真实来源、确认/共享策略签发、
PG、embedding、核心画像客户端与真实渠道尚未联合验收。新增画像 API 本身不代表 BOT
已经会读取第三人画像。

## 显式迁移

保持原 contract_directory 指向已发布 text-dialogue/v1。schema 1 仍可服务旧 v1。
启用画像存储前，停止所有写入进程，对明确的隔离配置运行：

```powershell
uv run tianshu-memory --config .runtime/demo/config.json migrate-profiles --backup .runtime/demo/before-profiles.sqlite
```

备份必须是不存在的本地路径；拒绝覆盖、拒绝数据库本身和网络共享。迁移在同一数据库
事务增加 profile_shares/profile_approvals 和元数据 schema 2，不自动公开旧事实。
迁移失败时旧权威表保持 schema 1，已成功创建的备份保留供核对。禁止把生成的数据库和
合成批准配置提交到 Git。

旧程序拒绝 schema 2。回滚先停止全部新旧写入者，再保留迁移后的完整库及 WAL，
将备份恢复到一个新的明确本地数据库路径，校验后用旧程序指向它。备份时点后产生的
数据不会包含在旧库中，应另行保留再评审；不提供丢弃新数据的原地降级或自动删除命令。

## 内部批准与发布

LocalWorkflow 上提供 `approve_profile(draft, context, expires_at)` 和
`publish_profile(draft, approval_ref, context)`，仅 local_fixture 可用。没有公开 HTTP
写入口；模型输出不能调用批准。context 仅由受信本地合成 issuer 提供。

draft 是 source_scope、subject（person/group）、sharing（group_only/public_preference）、
conversation_id（公开为 null）、category（人物interest/style，群topic/style）、field_key、
units。units 使用既有完整语义字段和原始来源引用，不能直接提交任意投影引用。
批准绑定完整 draft、来源当前 revision/epoch、到期时间，发布事务重查并消费一次；
重复同一批准仅重放同一结果，过期/来源已撤销时拒绝。批准期限限制未完成发布及重放，
不代表已发布兴趣自动到期；共享撤销通过权威来源更正/遗忘/撤回传播，生产共享策略撤销
仍需真实 issuer 接入同一失效事务。

public_preference 仅人物普通兴趣且需明确跨场景批准；私聊来源和群内本人主动披露来源
均可按该规则生成独立公共投影。已有 group_only 的批准绝不能复用于公开范围。
group_only 只用于精确群，群来源不能凭原批准迁到其他群。群主题的 source_scope 必须
该群来源，个人私聊不能被归纳为全群主题。群观察可保留 inferred，不转为确认事实。

生产授权可来自已确认的类别/范围共享设置，或已登记获准群观察策略，不要求逐条弹窗。
本任务没有实现这些 issuer；本地合成批准不是真实同意，也不声称任意群成员是群管理员。

## 查询与复核

请求人的 requester_scope 必须与即时 issuer 及稳定账号绑定一致；target 只确定查询主体，
不授予权限。跨人物只取当前受众可用的已批准投影。群 target 必须当前群的可信核心会话 ID。
没有昵称/头像自动关联，不提供其他人的私人目录/账号映射查询。

field: 精确字段先缩小已授权候选；其余查询使用同一词项覆盖/BM25选择器。每次预算包含
完整 selected_units 与 dependency_groups 的规范 UTF-8 装配字节数，tokens 为该值的保守估计。
零预算保留认证与范围/版本检查，不读取画像正文或来源正文。条件、否定、时间、推论程度
随完整组一起返回，超预算整组省略。unknown/无可读内容均为空/no_match。

`version_domain=profile-memory/v1` 的 scope_version 独立于旧 text-dialogue 域。
缓存及发送前复核必须用同一域；HTTP no-store。公开 epoch 加当前群 epoch，仅受实际
共享投影变更影响。其他群独有变化与目标私人记录数量/变化不进入当前版本语义。
多目标/多次补查由核心共用整轮预算；本接口不凭 query_text 猜同一轮。

完整本产品检查仍是 `uv run pytest -q`。新画像测试在 tests/test_profiles.py、
tests/test_profile_queries.py，负责组件数据/授权/排序；tests/test_profile_http.py 与
tests/test_profile_contract.py 覆盖 HTTP、独立进程重启、旧版兼容与发布包摘要校验。
