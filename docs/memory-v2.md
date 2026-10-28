# 长期来源检索

本版复用既有 records/groups/automatic_facts/source ledger、候选队列、来源权限与 context_operations。新的 `fact_subjects` 只维护来源约束下的稳定实体/事件标识；`memory_documents`、`memory_episode_index`、`memory_vectors` 是可重建派生索引，不是第二套事实账本。

## 当前写入与来源

普通自动学习只走 schema 6 多人分段整理，见 [会话经历整理与人物归纳](context-consolidation.md)。原 schema 3/4 的模型事实解析、语义修补与 schema 5 单人摘要已退役；候选端口不再协商旧协议或恢复旧任务。当前手工 `context/propose`、修订/遗忘与关系账本保留原归属。

经历和画像作为解释性材料，原话 episode 供精确检索与来源复验。它们共享现有数据库、来源账本和持久索引，不另建聊天库。模型结果只引用自己被分配的新材料；身份、范围、版本和权限继续由程序核验。

## 显式迁移和旧来源重建

先停写、保留运行配置与独立 source guard，再执行显式升级：

```powershell
uv run tianshu-memory --config C:/runtime/memory/config.json migrate-memory --backup C:/runtime/backups/pre-memory-v3.backup
uv run tianshu-memory --config C:/runtime/memory/config.json reindex-memory --batch-size 32 --max-batches 100
```

前置是 source schema 3、context schema 1、automatic_fact_schema 2；升级后 automatic_fact_schema 为 3。备份路径不得已存在，备份与 `.source-guard.json` 成对保留。SQLite 使用完整数据库备份；PostgreSQL 使用既有逻辑快照格式，记录实际已存在表的全部列/行和 guard。迁移不恢复已退役的旧自动整理入口。启动不自动升级。

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
