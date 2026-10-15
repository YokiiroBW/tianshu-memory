# 自动候选记忆处理

Memory 保有来源核验、范围、记忆事实和提交账本的唯一所有权。Companion 使用现有模型网关提议事实；模型不决定人物、角色、权限、关系分数或确认状态。

TS-168：候选排队时，另一条独立事实落库可能推进内容版本。自动来源抽取复用现有 history_version 区分纯新增与语义失效：仅前者允许非空提议继续提交；未知版本写入、修订或替换仍拒绝旧非空提议。空提议不恢复任何事实，可在原始来源仍有效时结束为 skipped。普通人工审阅的 commit_candidate 仍要求精确内容版本。

这些路径继续经过当前 SourceAuthority 屏障、精确 scope、来源 revision/epoch 快照和权限核验，并在提交事务内重读候选的 pending 状态。撤回或撤权后的候选不能借空提议通过。事件与原范围版本保持不可变，没有新增线协议、授权字段、生产权限或数据库迁移。

新增内部端口 `POST /internal/v1/memory/candidates/process`，由 TS-117/118 成对消费。沿用 Companion 的 Bearer 服务认证、`consume` 操作与事件精确范围/运行角色授权；不增加生产权限。来源必须使用已配置的 SourceAuthority。此端口不是浏览器接口，不接受 Cookie/Origin。

请求仅含 `job_id` 和 `output`。`output:null` 查询处理状态，未完成时读取来源权威当前允许的原文，返回 `{state:"pending",result:null,content:[{index,text,sent_at}]}`。已提交返回 `completed` 和原 `source-sync/v1 workflow#candidate_result` 回执；失效候选返回 `invalidated`。查询完成回执不会重新提炼。

提交的 `output` 为 `{"memories":[...]}`，最多 8 条，每条仅含：

- `statement`、`valid_time`：非空字符串，各最多 1200 字符。
- `conditions`、`negations`：字符串数组，最多各 16 项、每项 512 字符。
- `evidence`：1–16 个 `{source_index,quote}`，索引必须为有效整数，quote 必须是对应原文的非空精确子串。

请求体最多 65536 字节。Memory 在提交时重新同步并核验来源，绑定原事件的角色/人物/会话/受众与真实或虚构属性，以 `uncertainty=inferred` 提交 evidence 类别；不把来源存在或模型输出视为用户确认。空 memories 正常记账为 skipped 并释放候选容量。字段或引文不合法返回 HTTP 200 的 `{state:"rejected",result:{error_code:"invalid_input"},content:[]}`，这是允许重新提炼的明确拒绝回执。其他 HTTP/网络异常不等于已拒绝写入。

实际提交复用 `TrustedWorkflow.commit_candidate` 的 source guard、事务、去重、冲突与写入账本。相同 job 重放返回原回执，来源撤权不能靠重试恢复写入。后续查询、纠正、遗忘和关联仍走既有 memory-context/source-sync 能力。

本次无新表、迁移、凭据或外部依赖。先发布包含端口的 Memory，再发布 Companion 后台消费者；反序发布时 Companion 保留待处理任务并退避，不把候选受理显示成已写入。重启恢复时保留两个产品原数据库及 source guard。真实部署和模型效果须独立验收。
