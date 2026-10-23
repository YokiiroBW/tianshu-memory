# 规范事实与长期来源检索

本版复用既有 records/groups/automatic_facts/source ledger、候选队列、来源权限与 context_operations。新的 `fact_subjects` 只维护来源约束下的稳定实体/事件标识；`memory_documents`、`memory_episode_index`、`memory_vectors` 是可重建派生索引，不是第二套事实账本。

## 写入与兼容

现有 `/internal/v1/memory/candidates/process` 明确协商抽取版本。新读请求发送 `supported_extraction_schemas:[3,4]`；已锁定的旧 job 返回 3，未绑定的新 job 选择 4。提交携同一 `extraction_schema`，持久 job 与既有 write_ledger 控制重放；不能根据输出是否含 fact 猜协议或在 400 后降级。旧 `{job_id,output}` 保持 schema 3，旧空 valid_time 和原日期 offset 仍可读写。

schema 4 不收自由 statement。模型提供 subject/predicate/value、single/set、assert/add/replace/retract/confirm、精确 target_refs、qualifiers 和原句引用；Memory 渲染 statement。modality 为 asserted/observed/planned/conditional/uncertain，独立于 current/historical。计划不会被渲染成已经执行，来源报告已发生也不等于客观核实。实体 mention 必须在该项原句中，person/scope 来自受信事件；事实的 reality 同样由事件确定。真实与虚构槽相互隔离，可信本人姓名仍沿旧完整来源规则。

默认当前槽包含 modality，未指定目标的计划与已发生报告并行保留。explicit replace 验证同 owner、主体、predicate、reality、cardinality 的准确版本，可以由新来源报告改变 modality，且只退休明确 targets。confirm/retract 仍要求同 modality、同值；集合 add 保留其他成员。历史补充不会被当前单值互斥拒绝，也不能以历史 replace 退休当前值。单值未来生效由读取时的有效时间裁定。

作者姓名沿现有 `identity.name/person.name/author.name/name` 规则统一为 `identity.name`，兼容 key、field、渲染和更新目标保持一致；其他作者属性及实体名字不进入本人姓名槽。独立姓名断言必须由本次完整原文正向证明，转述、条件、否定或其他人的名字仍不能成为本人姓名。confirm/retract 从已核精确目标继承原正向姓名证据；服务器在既有事实 payload 中保存证据引用摘要，读取重新核对完整原文。控制意图仍是必须有效的来源依赖，不冒充新的姓名断言，也不能让 assert/add/replace 借用旧证明。

派生确认的有效期必须等于目标，撤回中非空日期只能继承目标相同日期；控制原话无需重报这些旧日期。新断言和 replace 的日期仍须本次来源支持。事实原有效期与控制发生时间分开：确认/撤回退休目标的时刻使用新控制来源的 observed_at。全部继承来源沿既有 lineage/evidence 保留，原来源遗忘、撤权、迟到任务和重建仍统一失效。

retract/confirm 的旧值来自精确目标，结果组继承目标的完整来源 revision/epoch 和引用证据，包括前序更新已继承的依赖。retract 必须匹配目标值，confirm 还必须保持状态、有效期、条件和否定项。确认组保留同一值及证据，旧目标成为可审计前驱，避免随后撤回确认时原副本重新成为当前值。独立 assert/add/replace 的新内容不继承被操作目标的来源；schema 4 仅在相同来源基础内合并独立同值事实，不能把一个新来源声明并入带旧来源依赖的组。旧 schema 3 的既有语义去重不变。

旧记录保持 `legacy_unstructured`。新明确来源可以建立规范当前值，旧值不会阻塞 assert；同值也不把旧 mixed statement 合并升格为 canonical。当前投影在同槽和时间可证时优先规范事实，原记录与原文继续可查。模型仍可能误解自然语言；引用存在和结构正确本身不证明语义蕴含。

## 显式迁移和旧来源重建

先停写、保留运行配置与独立 source guard，再执行显式升级：

```powershell
uv run tianshu-memory --config C:/runtime/memory/config.json migrate-memory --backup C:/runtime/backups/pre-memory-v3.backup
uv run tianshu-memory --config C:/runtime/memory/config.json reindex-memory --batch-size 32 --max-batches 100
```

前置是 source schema 3、context schema 1、automatic_fact_schema 2；升级后 automatic_fact_schema 为 3。备份路径不得已存在，备份与 `.source-guard.json` 成对保留。SQLite 使用完整数据库备份；PostgreSQL 使用既有逻辑快照格式，记录实际已存在表的全部列/行和 guard。旧事实不重抽、不猜旧模态，旧 jobs 锁定 schema 3。启动不自动升级。

`reindex-memory` 从当前仍有效且有权限的旧来源回读精确文本，验证读前/读后的来源 revision/epoch 后建立 episode 和事实索引。`memory_reindex_cursor` 在现 metadata 中保存进度；`completed` 表示扫描结束，`unavailable` 非空表示这些来源尚未成功，可重试相同命令；`--restart` 从头重新核验。重建不会生成新的事实。迁移后新的输入会直接建立来源片段，旧来源需此显式重建才能进入长期原话检索。

## 查询、验证与遗忘

`POST /internal/v2/memory/context/query` 最多 4 个结构化子查询，默认总预算 8 个事实、8 个 episode，多个子查询共用 budget/limits。事实保持完整语义组，episode 返回精确 UTF-8 span、来源观测时间和 reality。未知事件时间保持 null。多 owner 由当前授权域中的姓名证据解析；一部分名字未知时保留 partial/incomplete，不扩大到全群。

候选索引按稳定 ID 每页 512 条遍历完整授权域；不会先截掉最近 4096 条。时间约束在 lexical/vector top32 之前筛选候选，未知时间保留并标不完整。episode 候选阶段只读元数据和持久向量，选中后才装配原话。FTS 与持久向量共同排序，来源核验继续走既有完整屏障。没有新原文不代表用户未说过，历史完整性和检索不完整状态会明确返回。

查询先在完整来源屏障事务内准备候选与现有向量。空候选、纯词法选择、尚无可用向量的词法回退，在同一个事务内装配证据与选择证明；配置为 hybrid 并不自动触发第二轮屏障。只有本次确实需要查询嵌入或重排时，才释放来源事务执行后端工作，再取得第二次完整新鲜屏障并核对每个子查询的完整候选域。外部工作期间的目标来源撤回、域内新记忆或版本变化仍使旧选择失效，向量损坏仍明确标不完整并允许重新索引。

子查询的归属解析、选择、两阶段复验和 bundle 验证共用 `query_domain`。self/named/multiple 只在既有完整授权集合中按解析出的 owner 选择 scope，保留明确批准的直接私聊关联；room 使用完整授权 room，未知目标不扩为 room。摘要绑定每个精确 scope、关联 ID/version、全部 active groups/record versions、episode revision/epoch/hash 和完整性，不按相关词、时间或排名缩小。无关其他 owner 的新原话不会使 self/明确目标失效；目标 owner 的新原话即使未抽出事实或没有查询关键词，仍使旧选择失效。每次仍在完整授权 NameView 重算姓名唯一性、歧义和未知目标，姓名见证新增同名、改名或撤回会被检出；未批准私聊不会因 person 相同而进入域。

`POST /internal/v2/memory/context/verify` 验证 bundle 以及最多 64 个 select/history/input_history/profile 旧检查。先收集完整依赖域，再执行一次完整来源屏障，内部读复用这个事务快照；保留全局来源锁、版本 CAS、姓名唯一性和无结果的完整候选约束。每次模型/发送边界仍需新验证，不能以 TTL 复用。超过 64 个旧检查由调用方分批，不额外阻断用户。

不透明 bundle_id 承载服务端 selection 的 HMAC 证明，覆盖查询、预算、限制、选择约束、全部证据与来源依赖。公开 evidence_digest 不能自行改成一份无结果证明。证明 key 位于现 metadata，由显式迁移创建，随后续备份保留；不进入模型或公开诊断。verify 验证证明后仍重做当前来源/候选域检查，签名不替代权限或新鲜性。

`POST /internal/v2/memory/context/forget` 使用 kind=forget_episode、episode_ref/version、当前指令 evidence_refs 和既有 Platform revision proof。用户证明绑定完整语义请求；沿 `context_propose` 权限和原 scope。一次忘记撤回目标来源的所有重叠 span、向量、FTS、相关事实与迟到候选，并更新无事实来源的历史版本。没有事实的来源也能遗忘，无需伪造 record。响应和超时恢复复用 v1 receipt 与原 context_operations；source suppression 阻止重建或迟到嵌入复活。

来源级 forget_episode 的 receipt 保持 `record_ids=[]`、`group_ids=[]`：精确 episode/source 目标由原请求语义摘要绑定，并不受旧单组回执容量限制。实际清理包括所有依赖该来源的派生组，完整失效状态和历史审计仍在既有记录中；空 IDs 不表示没有清理派生事实。直接针对 record 的旧 forget 协议不变。

## 可选本地 CPU 向量后端

配置明确的已安装隔离 Python 和本地模型目录，不向整个产品加入 torch/transformers，也不下载模型：

```json
{
  "retrieval": {
    "backend": "local_python",
    "python_executable": "C:/runtime/embedding-env/Scripts/python.exe",
    "model_directory": "C:/runtime/models/bge-small-zh-v1.5",
    "model_revision": "7999e1d3359715c523056ef9478215996d62a620",
    "dimension": 512,
    "mode": "hybrid",
    "rerank": false,
    "candidates": 32,
    "threads": 2,
    "timeout_seconds": 45
  }
}
```

该后端使用显式 UTF-8 子进程、离线 safetensors、CPU CLS 分块平均后归一化；查询使用固定中文 query prefix。embedding_revision 包含实际模型资产摘要、归一化/分块和前缀策略，模型更换不会复用旧向量。

已升级服务生命周期按每批 4 条补齐缺少的文档向量，持久键为 document ID、content hash、embedding revision。重启复用已完成向量；只对变化文档增量重建。也可显式运行 `reindex-memory --vectors --batch-size 4 --max-batches 100`。一次查询最多共享 8 秒向量等待预算，文档索引使用配置的超时；抢锁、写管道与读回均受同一调用 deadline 约束，失败会终止并回收 worker。

worker 超时/退出或损坏向量不会变成 no_match：仍有词法证据时正常返回并标 `index_incomplete`，损坏派生向量将重新排入现索引补齐过程。来源/权限错误仍拒绝读取。`/health` 只报告 configured_not_verified，配置存在不等于模型实际可用；真实质量、原生 PostgreSQL 和设备运行由协调者独立验收。
