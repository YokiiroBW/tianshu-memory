# TS-032：真实来源接线最小候选（待协调审查）

此文件记录固定提交事实及后续最小接线要求，不是共享 wire、已实现适配器或完整来源验收。
TS-032 只实现 HTTPS 身份解析的显式 CA 配置。`configured_app` 正式模式仍传
`source_authority=None`；不得把 Platform 的部分检查或 Core 的短期上下文布尔检查直接
替换为 Memory 的完整来源权威。未访问其他任务的可变目录或数据库。

## 固定证据与事实归属

| 产品/固定提交 | 实际入口与事实 | 不具备的保证 |
| --- | --- | --- |
| Platform `a5ee59ff67a2de7a7e0d8328ea3c0f43e1d6a209` | `services/platform/server.py:create_app` 公开两个 POST：`/internal/v1/origins/resolve`、`/internal/v1/model-config/snapshot`，仅 loopback、拒绝 Origin/Cookie；`Origins.resolve` 以解析服务凭据的 service 决定 receiver，以 resolver.caller/purpose 决定调用者和用途，复核登记入口摘要、路由、owner、到期与撤销。 | 本地 rehearsal 的登记与认证，不是真实浏览器会话/渠道事件证明；无来源快照 HTTP 端点。 |
| 同上，`services/platform/origins.py` | `prepare_mapping`/`confirm_mapping` 是应用内部端口，绑定 request_id、账号、渠道与真实上游响应。Memory 的 register/resolve 响应提供 person_id/binding_version，Core ingest 响应提供 conversation_id；平台保存映射并核对冲突。`observe_source` 是受信 rehearsal 事件适配端口，按 entry 与 message_key 维护 revision/tombstone。 | 这些方法均未公开为 HTTP wire。不能读平台表代替端口、不能让普通 payload 自报映射或据此宣称生产账号接通。 |
| 同上，`Origins.verify_current_sources(header, event)` | 只接受已认证 companion 发布者，复核 event 的会话/scope、逐来源当前登记入口/路由/映射/revision/tombstone，来源无记录为 503。返回 `current_sources=True, archive_verified=False, scope_version_verified=False`；任何 archived 来源直接 503。 | 不比较 source.receipt_id/locator，不校验 Core 轮次实际输入或真实发送事实，不拥有 Memory scope_version；不能当 `verify(db, sources, scope)` 或 `current(db, rows)` 的直接实现。 |
| Core `812019e287a5bf37d9b5d810a028ffea828b872e` | `src/tianshu_companion/core.py:ingest` 核验账号/渠道/actor，建立 channel → conversation，按修订记录实际受理输入、receipt_id 与撤回，写 source 时固定 `archive_state=pending, locator=None`。`store.py:latest_source` 取本会话该消息最高修订；`short_context.py:sources_current/select_recent` 核对当前修订/撤回、context_revision、账号绑定、scope、真实 sent 回执，筛选短期上下文。 | `sources_current` 仅为 Core 本地函数，不是来源查询 HTTP 或跨产品授权凭据。短期受理回执不能证明永久归档。当前 app 仅 ingest/cancel 业务 HTTP，无独立源快照/修订订阅/归档验证入口。 |
| 同上，`core.py` 封账与发送复核 | 非旧 edit/retract 增加 context_revision，取消或失效对应轮次。封账持久化 turn_committed outbox，携带本轮输入与真实发送状态；拿不到 Memory scope_version 时保留 blocked_scope，明确不编造版本 1。 | `context_revision`、input_revision、aggregate_version 分别用于不同事实；均不能等同 Memory scope_version。committed_event 也不覆盖事件产生之后的撤回。 |
| Memory 基线 `dbf5c196dbb6de0d8828c0466e824efb5c7d08ce` | `service.py:_source_rows` 调用逻辑来源端口 verify，随后要求本地 sources 行的完整 source、revision、scope、state/reality 相符；`_group_current` 以 lineage 的 required_revision/required_epoch 调用 current。`sources.py` 唯一实现是 `LocalFixtureSources`，verify 查本地 ledger，current 比较 active/revision/epoch。 | 没有现成生产 SourceAuthority 类型或实现，不能仅新增一个返回 True 的类。只做远端布尔核验还不能填充可信本地 sources，也不能覆盖已经返回的零预算版本探针。 |
| 同上，`workflow.py`/`profiles.py` | LocalWorkflow 明确限定 fixture：来源改变与失效组/候选同事务；更正/遗忘有 payload 绑定确认；共享发布另需主体、类别、范围批准。画像来源血缘不对读取者公开。 | 用户更正确认与共享批准的生产 issuer 未接；Platform dialogue 路由、服务令牌、模型建议都不是这两类批准。 |

以上以 `git show <固定提交>:<路径>`、`git grep <固定提交>` 只读核验，未运行其他产品。
合同依据为根已发布 `text-dialogue/v1/semantics.md` 第 1/2/5/6 节以及
`profile-memory/v1/semantics.md`；不修改已发布文件。

## 最小接线候选与缺口

1. **身份通路先行。** Memory 的出站 HTTPS resolver 凭据在 Platform 对应
   service=memory、resolver={caller:companion,purpose:dialogue}；Core 对应
   service=companion、resolver={caller:nonebot,purpose:dialogue}。登记入口分别允许
   nonebot → companion 与 companion → memory 的 dialogue 路由。初次 person/conversation
   都可为空：先凭已验证账号做 Memory resolve/register，凭已验证渠道做 Core ingest，
   经平台现有 prepare/confirm 应用端口处理对应真实响应后回填；禁止客户端直接提交
   “已验证”ID。内部端口若需跨进程承载，先由协调者确定可信适配器及合同，不能自造 URL。

2. **Core 提供其实际拥有的源事实读取和修订事实。** 最小需要按稳定 message_key（去
   revision）、获准会话精确读取当前受理 revision、撤回、scope/作者/actor、受理 receipt
   及归档状态，并验证所宣称 turn/input_revision 与真实输入集合相符。返回应能关联请求、
   标明版本/读取水位，不能只回一个 current 布尔值。Platform 的登记、用途、权限撤销
   仍由 Platform 负责；Core 的受理事实不能取代权限。具体字段、认证、错误和版本订阅
   由双方与协调者发布，当前没有这个 wire。早期 pending 来源可以保持 pending 使用，
   但 receipt 必须与 Core 的真实受理事实匹配；archived/locator 只能来自真实 Chat Audit
   回执验证。此次未核验 Chat Audit 接口，不为它指定猜测的地址或能力。

3. **Memory 持有派生状态及本地原子失效。** 适配器必须把受认证的源事实映射到现有
   sources 的完整 payload、scope、reality、revision、epoch、state，并在同一 Memory
   事务中失效 lineage、语义组、投影、候选和相应范围版本。生产源同步需要独立受信入口；
   不能将只允许 fixture 的 LocalWorkflow 直接换名投入生产。reality 还需核验其可信
   来源及混合情境，不能因事件 owner 合法就默认全部现实。

4. **读前一致性不能仅靠后台事件。** 仅让 `_group_current` 远端查一次不够：零预算
   select、profiles/select 在读 scope_version 后直接返回，不调用 current，迟到撤回
   也可能使旧 epoch 看似有效。最小候选是 Core/Platform 拥有各自单调事实水位和可补拉
   失效记录，Memory 在版本探针及候选读写前建立可证明的读取屏障，再在本地事务内应用
   到该水位并读取版本；权限撤销与源修订都必须覆盖。不可证明已追平、出现序号缺口或
   远端失联时返回 503，不能服务旧私密缓存。该协议尚缺双方接口和竞态验收，不能声称
   跨进程原子，更不能用一个 TTL 宣称撤回已即时生效。若业务要求强于屏障读取的并发
   保证，需另行确定协调/租约机制，不能暗中降级。

5. **保持单向依赖。** 来源读取只依赖 issuer 的登记/权限及 Core 已受理事实，不调用
   Memory select/consume，也不等待 turn_committed。Memory 负责产生自己的版本，Core
   发送前查询它并将结果带入封账事件；不能要求第一条来源必须先由 Memory 接受一个
   已带 scope_version 的 committed_event 才可查询该版本。这会把初始化再次绕成循环。
   mapping 也依据真实 resolve/register/ingest 回执，不要求先有记忆条目。

## 版本、撤回、更正与遗忘的分工

| 事实变化 | 权威与传播要求 |
| --- | --- |
| 原消息 edit/retract | 入站适配器证明渠道行为，Core 持久当前修订/撤回并使短期上下文与轮次失效；可信修订同步到 Memory 后同事务提高 source epoch、排除受影响语义/投影、使候选过期。Platform rehearsal observe_source 不能替代真实入站凭据。 |
| 来源引用/入口/身份权限撤销 | Platform（真实渠道接入时相应 issuer）持有撤销事实；Memory 每次身份解析复核，后台整理还需稳定来源权限验证，不能依赖已过期的短期 assertion_ref。只撤销 origin 本身和撤销底层来源访问权不是同一事实。 |
| 用户已确认 correct/forget | Memory 持有确认与派生记录/墓碑、record_version、来源 epoch、scope_version；先同事务失效语义/投影与候选，再异步索引。确认必须绑定当前账号、完整目标/语义摘要和有效期；当前仅 fixture 支持。不能自动当成删除 Core 或 Chat Audit 的原始记录。 |
| 真正删除原文/归档与保留政策 | 相应原文 owner 执行并回执，协调确认传播范围；本次没有原文擦除协议。Memory 的 forget 不等于跨产品物理擦除已完成，也不能由旧归档回执或晚到事件恢复墓碑。 |
| 群/公开共享批准及撤回 | Memory 的共享授权独立于普通来源读权限，生产需可信本人设置/群策略的批准来源。私密变更仅在影响仍公开的投影时才推动共享 epoch，不能泄漏隐藏目标/已撤回画像后续私密活动。 |

Memory `text-dialogue/v1` 的 scope_version 属于精确 actor/person/audience/conversation
元组，缺行初始为 1；修改由 `_bump` 在本地事务内单调推进。画像固定
`version_domain=profile-memory/v1`：私聊读该 actor 的公开 epoch；群读公开 epoch +
当前群 epoch − 1，与 target 是否存在无关。不得拿 text-dialogue scope_version、Core
context_revision、消息 revision 或平台映射 binding_version 替代它。发送前的零预算
known_scope_version 探针必须使用对应版本域，并共享一轮预算策略；Core 812019e 尚未
消费新增画像 API。

## 下一次双方验收应能证明

- 固定产品实现与真实 HTTPS 上的 origin register/resolve 成功；错误 CA、主机名/用途、
  resolver token、caller/receiver、入口用途、actor、引用过期与撤销均失败。
- 真实 Core 受理 pending source 可核验；伪造 receipt、旧修订、撤回、错 scope、假 archived
  不可入候选或召回；晚到 committed_event 和后台候选不能恢复失效项。
- 先取普通/零预算/画像探针，再使源或授权撤销：读取屏障后旧版本应冲突或明确不可用，
  在水位缺口/断连/重启窗口不返回旧私密数据；两种 Memory 版本域独立。
- 仅私密变更及已撤回画像之后的来源更正不泄漏共享 epoch；完整受影响组同取同舍，
  已确认遗忘和真实原文删除的回执分开记录。

上述后续验收并未在 TS-032 完成。TS-032 的 TLS 测试使用合成 issuer 业务响应与真实 TLS
传输；TS-050 的固定版本跨产品 partial 结果由其任务单独记录，不合并成完整 L0 声明。
