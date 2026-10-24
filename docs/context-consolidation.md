# 会话经历整理与人物归纳

普通新学习使用 `memory-extraction` schema 5。Core 沿已有后台队列逐 job 整理，每个 job 提供真实 owner 读取的有界多轮会话窗口，包含明确作者的人类原话及可证明已送达的 BOT 回复。每次只覆盖当前 job 的完整新输入，旧轮次仅作为上下文，不因此冒充已处理任务。窗口最多 12 个 turn、32 个完整成员和 48KiB 正文，不截断新输入。Memory 不另建聊天库、工作者或来源权威，也不把 BOT 发言归为用户事实。旧 schema 3/4 的持久任务恢复和明确精细操作保留原行为。

Memory 先通过原来源端口捕获新输入的完整原话 episode，再冻结该 job 的待整理窗口和画像输入。重复及 unknown 恢复先查自身真实回执；只有尚未认领的 pending job 才返回 `unclaimed`，允许重送完全相同的原窗口，不重新选窗。模型仅提供 `summary`、可空的 `portrait` 和可选三档 `importance`，不能指定作者、权限或证据。摘要是带来源的经历解释，画像是基于多段经历的可修订归纳；两者都标记 `inferred`，不承诺逐句事实裁决。至少存在一段既有独立摘要，再加本次新经历，才提供画像整理输入。

首次读取尚未认领 batch 且明确只有旧 context 失效时，返回绑定该窗口和任务集合的 `context_changed`，没有完成回执或游标。Core 可在模型调用前移除完整失效 context 单元后重捕获一次。Memory 必须独立确认新输入仍当前有效；新输入、身份/权限、配置、数据库或不明错误不按此降级。协商到 schema 5 的任务按自身新来源和已认领窗口依赖失效，旧 schema 3/4 保持原全域语义；已经认领或 unknown 的窗口不能替换。

接收和提交窗口各核一次 Core owner 窗口证明；日常查询复用同一 SourceAuthority 的批量 source-facts 屏障，以每批最多 32 个 turn 和 256 个 selector 复核。只有同窗完整包含该 turn 人类输入时才采用其 BOT 回复。捕获时 owner 已验证其 sent 资格、固定正文/结果版本和稳定回执投影；之后依赖 Core 对 sent 不降级、同 reply 的 channel IDs 不变、正文/结果版本不变的约束，用当前 `reply_ids` 和完整输入版本复核。稳定回执摘要只覆盖 `{reply_id,state:'sent',channel_message_ids}`，不包含可变化的轮询时间或 receipt ID。

摘要、画像和原文沿用现 groups/records/lineage、write ledger、文档和向量投影。窗口中的全部人类来源与实际使用的 Memory 依赖由服务端固定；旧画像文本的完整叶来源闭包也继承，正常替换旧画像不会要求旧指针永久 current。来源撤回、身份绑定变化、关联权限撤销或显式忘记会阻止晚到提交，并使派生内容失效。画像 CAS 冲突可以返回画像 `pending`，已经保存的摘要和原话仍有真实完成回执。覆盖游标只随当前 job 的真实完成回执推进，不认领或完成其他 job。

整理沿用原 bundle 的 256 个完整来源预算。新窗口先占其完整叶来源，再按近期完整摘要选择画像输入；只有旧画像的全部叶依赖仍放得下，才输入其全文，否则省略旧画像，从近期摘要重建。不能截断已使用文本的来源叶。没有一条既有摘要可完整容纳时不提供画像输入，新经历只依赖自身窗口，形成新的有界整理起点。这是可修订的近期归纳，不承诺终身完整画像。

查询仍使用 v2 `facts`/`episodes` 两槽。摘要和画像与 typed facts 合计占原 facts 上限；不经 typed 姓名解析才可作为上下文候选，每条保留真实 owner。其全部叶来源 owner 必须在原授权 frame 内，并一起占原 17-owner 预算；放不下就省略整条候选，仍可返回其他材料。新派生记录不能经旧 `profile_evidence.prepare` 被裁为仅本人 raw refs 的公开画像草稿；本范围后台画像不增加公开权限。

v2 `retrieval_mode=context` 返回当前已授权 frame 中的相关正向材料，不做文本人物消歧，不承诺最新、唯一或穷尽。每个结果恒为 `incomplete` 并含 `context_only`，约束只有 `positive_sources`，没有姓名 witness 或空结果的 `no_match` 保证。缺省以及明确 `factual` 保留原集合、姓名与负向证明；缺省请求的原 HMAC 形状不变。`subquery.intent` 仍唯一决定 current/history/mixed 时间范围。

context bundle 的 HMAC 固定模式、查询、预算和实际选中材料的完整依赖；scope/history/association 版本属于复验时重新计算的当前观测。`verify` 和 `prepare` 继续经过原完整 SourceAuthority 屏障，从当前授权 frame 重建选中原文、记录及全部来源叶，核对正文摘要和精确版本。无关 append 不改变已选集合身份，实际被选来源的撤回、更正、忘记或关联撤销仍失败。旧期限不授予新权限。需要外部重排时，其后只采用同身份、同版本、同内容的原候选，从 fresh frame 装配正文；变化候选整条省略并标记 `stale_source`，不补新候选、不重跑 backend。此阶段仍做完整来源同步，不宣称已减少全域同步 RPC。

运行 JSON 可配置：

```json
{"memory_consolidation":{"ttl_days":{"low":7,"normal":30,"high":180}}}
```

服务端 recorded_at 决定摘要及画像的 expires_at；schema 5 覆盖的新原话来源也保存同次重要度和期限。context 复述不续期。自然到期仅退出 `subquery.intent=current` 日常候选，`history`/`mixed` 可查自然到期内容，仍严格遵守权限、来源和 suppression。它不删除原文/索引、不写 suppression，也不撤销画像的既有经历依据。未被 schema 5 覆盖的 legacy 原话保持原行为。Chat 精确历史回读 adapter 尚未接通，只保留原有 archive 引用。

原 `migrate-memory --backup <新本地备份>` 继续作为显式停写迁移入口。它在原 schema 3 检索投影迁移中安装 `memory_consolidation_schema=1`；已有 schema 3 数据库再次调用时会先制作新的备份及 source-guard 副本，再增加 batch、依赖和生命周期元数据。迁移不会自动重抽取、猜测旧记录时间或批量改变旧内容。

本功能块的本地/原生/联合验证状态以 TS-306 交接记录为准。
