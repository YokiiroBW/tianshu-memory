# 记忆检索优化与事件投影

本批沿既有 Memory/Companion 实现：二进制与 JSON 向量双写、按完整 owner scope 检索、词法派生索引、重要度/时间排序、完整选材单元配额与 MMR、一次最多 80 条重排、结构化保留期、真实材料轮转、重复提示、明确引用的事件状态。原授权、来源 checkpoint、抑制、原子提交、公共合同与模型保持。

已有库在停写后依次执行以下显式迁移，每个 backup 都必须是不同的新本地文件，数据库和 source-guard 成对保留。新装的 PG 建表也使用相同 DDL；启用功能仍按各 marker 显式安装，不在普通读路径偷偷迁移。

```text
tianshu-memory --config <private-config> migrate-retrieval --backup <new-file-1>
tianshu-memory --config <private-config> migrate-partitions --backup <new-file-2>
tianshu-memory --config <private-config> migrate-rotation --backup <new-file-3>
tianshu-memory --config <private-config> migrate-merge-candidates --backup <new-file-4>
tianshu-memory --config <private-config> migrate-events --backup <new-file-5>
```

向量仍使用原 embedding_revision 和维度，未新增依赖或 PG 扩展。二进制是完整 JSON 的 float32 副本；PG 迁移备份中 bytes 可为空，完整 JSON 不丢。切回 R35 的固定五列旧写者前，须在停写状态用 `retrieval_migration.rollback_binary(store, new_backup)` 备份并恢复五列布局；不覆盖旧数据库或手改 guard。其他派生表可以留存，由旧版忽略。

Memory 私有配置的 `retrieval.trace=true` 开启有界内存追踪，默认关闭。原独立诊断 token 可读取 `GET /internal/v1/diagnostics/recall-traces`；trace 不进入公共 bundle 或固定格式运行日志。候选数、真正通道命中数、RRF 数、降级、选材预算和最终 ID 分开记录。离线 `scripts/recall_diagnose.py --fixture ... --dry-run` 分别报告证据召回、完整集合召回与有标注时的 oracle 回答正确率；未标注时为 null。

重复与事件状态属于派生提示。私有候选 hints/event-state 入口复用既有 companion consume 身份、准确 job/batch/owner 和实时来源检查，不增加公开合同字段。相似度不自动删改；没有新向量时先保留，等既有索引完成后才能得到改写相似提示。事件只关联同 scope 的明确 prior_refs；自然语言中没有明确结构的任务、人物或偏好不猜测。第三方报告不升级成本人承诺，未明示更新的主张并列；valid_from 未知时保留 null。

每卡实际结果和回滚见 docs/handoffs/M0.md 至 M3-2.md。固定 600 文档的 owner 候选阶段微基准已测，不能替代带来源 I/O 的生产端到端性能和真实回答质量。公共包仍最多 17 owner、facts/episodes 各受原预算限制；数据规模超过槽位时不承诺一次包容纳全部人。可选 M1-2/M3-2 未启用。
