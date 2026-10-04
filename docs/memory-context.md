# 连续上下文服务

`memory_context` 扩展现有 Memory 的记录、语义组、来源 lineage、修订、FTS 和事务，不建立第二份事实、关系或正文库。角色开放事项仍由其既有 owner 管理，短期情绪不写长期账本；计划与事实由 draft unit 的 reality、valid_time、uncertainty 保持区别，模型提议不构成用户纠正或账号关联的同意。

服务消费协调者发布的 `memory-context/v1`；固定 manifest 及全部传递依赖只由 `src/tianshu_memory/contracts.py` 持有和严格检验。部署须提供同批完整合同目录，配置 `contract_directory` 指向其中 `text-dialogue/v1`。不得用旧合同目录通过关闭校验启动。

## 正式入口

以下均为 POST `/internal/v1/memory/context/<operation>`，JSON 必须符合正式合同。调用方 bearer token 只标识服务；`query.origin`/`command.origin` 经既有 Platform issuer 实时解析，仍核对稳定账号绑定与精确 actor/person/audience/conversation。新操作复用现有准入、正文限制、超时和诊断，不接受浏览器 Origin/Cookie。

| operation | caller operations 权限 | 作用 |
| --- | --- | --- |
| query | context_query | 自然文本或既有 field:/item: 精确选择；按整组共享 tokens/bytes 预算 |
| propose | context_propose | 一项 upsert/correct/forget/no_op，独立幂等键和可查询结果 |
| receipt | context_receipt | 按原 operation_id 找回 ACK，不重做已提交项 |
| batch | context_batch | 按 batch_ref 分页列出已受理逐项结果，支持日终增量反思 |
| association | context_association | 证明两主体同意后关联两个精确私聊范围；任一主体可撤销 |

自然文本召回使用现有中英文索引词，无固定“记得”开关。每个授权范围只取有界候选，合并后共用整轮预算，保留条件、否定和同组成员。`coverage.matched_groups` 是有界候选观察值，`complete=false` 表示截断、预算不足或时间缺口；`history_complete=false` 始终不表示全量历史已读。

仅 `time_range` 非空时按半开 UTC 区间 `[from,to)` 核验原消息 `physical_input.sent_at`。复用来源 HTTPS facts/current 与 snapshot/access/head 校验，整轮至多临时取 256 个 selector 的正文，不持久化原文；补传 accepted_at 和 turn.occurred_at 不代替发送时间。缺少时间的候选保留，返回 `source_time_unavailable` 和 `coverage.missing_source_times`，调用者不可说这些候选已被证明发生于范围内。普通回忆的 `time_range=null` 不作时间过滤。大正文超过既有来源响应限制、owner 移动或授权事实不一致均明确不可用。

关联仅允许直接的两个受证明私聊范围，不合并 person，不传递关联、不扩大到群。读取同时返回 `scope_checks` 和 `association_version`。消费者在准备发送前以原 checks/版本、零预算 query 复核；任何事实或关联变化返回 scope_changed，须重新组装上下文。

## 提议、证明与恢复

upsert 需要完整非空 units 与 evidence_refs；写入复用 `_WorkflowBase._write_group`。correct/forget 需要目标 record/version CAS、真实纠正 evidence 与 purpose=revision 的证明。自然聊天纠正由 Platform 从当前真实入站作者或既有用户操作、明确纠正意图签发该次 payload 的 proof_ref，不要求额外网页点击。correct 在同一事务撤回旧源派生、阻止迟到反思，再用仍有效的新证据写完整新组；失败整体回滚。forget 保留墓碑/history/lineage，不清空关系账本。no_op 只表达无可提交的长期事实，units 必须空；无新事实时 evidence_refs 可空，不要求 proof，不消耗事实来源。

每项受理后的领域拒绝保存 `rejected` 回执，其他项可继续。版本冲突、不存在、无效组或无权提交不会只报 HTTP 500。服务/来源/证明依赖不可用不形成终局拒绝，身份或 schema 未通过也不生成回执。相同幂等键语义不同报 409。超时或断连后执行可能已开始则返回 execution_state=unknown、retryable=false，消费者先查 receipt，再用原键恢复；不得换键重复提交。回执及批次只向同一已认证服务、精确 scope 开放。

Platform 必须实现 POST `/internal/v1/memory-context/proof/verify`，签发/验证约定在正式合同 README。配置增量为 `memory_context_proofs: {url, token, ca_file?}`；url 为该路径的 HTTPS 完整地址，凭据只在私有配置，ca_file 必须是绝对路径。Memory 校验 response schema、原 request/ref/purpose、规范 payload SHA256、真实 accounts/scopes 和期限。关联用途必须是针对本次绑定挑战和 scope 的两个主体分别确认；两条任意 QQ assertion、昵称、模型自述或服务 token 均不足。proof 未配置时只有需证明的动作返回依赖不可用，查询/upsert/no_op/已有关联撤销仍正常；不得据此永久取消聊天纠正能力。原 `/identity/link` 也接同一证明并创建范围关联，保留原 person 和 binding。

## 显式升级

停写后以既有 schema 3 和完整合同执行 `uv run python -m tianshu_memory.cli --config <私有配置> migrate-context --backup <新的本地备份路径>`。新增 metadata memory_context_schema=1、逐项回执/来源目标应用/关联/关联版本表；所有权威表继续受既有 source-guard 跟踪。备份同时保存完整 SQLite 与配套 `.source-guard.json`。已有 source_writes/write_ledger、账号、记录、history、lineage、relationships 全保留；仅可证明 lineage 的旧产物映射入目标应用表，不猜旧账本归属。

升级后新服务逐项提交走 context_applications，来源可支持不同目标；旧候选工作流也按目标及实际产物去重，重复目标不吞掉同批其他新目标，source_writes 保留原历史。没有 context 升级的旧库继续原既有工作流，新入口明确不可用，不自动迁移生产库。恢复必须停写并走既有 source-guard 恢复审查，不能仅覆盖旧 DB 或重建 checkpoint 擦除后续撤回和关联状态。

本地专项：`uv run pytest tests/test_memory_context.py tests/test_memory_context_runtime.py -q --basetemp .runtime/tests-context`。HTTPS owner 与 proof 使用明确合成替身，服务路由、SQLite、HTTP 进程停启及 TLS 传输真实运行；这不替代 Core/Platform/Companion 联合接线或真实用户验收。
