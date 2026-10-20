# Memory / Knowledge 可选业务存储

默认沿用本地 SQLite。`tianshu-storage==0.1.0` 以 `vendor/` 中固定 wheel 安装；产品拥有原生 PostgreSQL schema、检索、来源检查点和迁移，公共包只管理连接、后端选择、可选 Redis 客户端与不可变文件。

## 启动配置

保留 `database_path`；它还是本地 SQLite 接续来源、状态文件与独立检查点的路径依据，不填写 PG URL 到这个字段。

```json
{
  "database_path": "/srv/tianshu/memory.sqlite",
  "knowledge_files_directory": "/srv/tianshu/knowledge-files",
  "storage_backends": {
    "postgresql_url": "",
    "redis_url": "",
    "namespace": "tianshu"
  }
}
```

`knowledge_files_directory` 可省略，默认 `<database_path>.files`。容器可通过 `TIANSHU_POSTGRES_URL`、`TIANSHU_REDIS_URL`、`TIANSHU_STORAGE_NAMESPACE` 覆盖对应非空值。配置、凭据、TLS、合同和项目资料根仍由既有挂载提供。不要把含凭据的 URL 写进仓库、日志或交接。

首次外部连接不可用时保留 SQLite，状态如实标为未启用。成功连接会测试目标读写回滚权限并执行产品 schema / 导入；只有主库和 `role_grants_database_path` 指定的必要准入分库都准备好才完成激活。Memory 服务使用 `memory` 域，独立资料服务使用 `knowledge` 域，准入分库是单独逻辑 schema。

两个独立部署各用自己的物理数据库/状态目录；容器内路径文字相同不代表挂载相同。历史上若两个入口共用同一 SQLite/guard，PG 接入时同一状态锚点已有另一服务的 pending/active PG 标记会明确拒绝，不能自动复制成两个权威。该历史共享场景需先安排明确的数据归属，不在本卡内自动拆库。

PG 激活状态保存在 `<database_path>.storage/`。以后断线、移除 URL、目标归属变化或丢失绑定都失败关闭，不能写回旧 SQLite。连接与迁移错误只返回脱敏状态。Redis 失败则旁路；本卡仅接入可选客户端识别，未新增共享向量缓存、权限缓存、可靠队列或锁服务。

## SQL 与文件归属

- SQL 保存人物/账号绑定、范围、可编辑记忆、修订、来源关系、抑制和消费账本、幂等回执、outbox、好感/冻结/衰减、资料目录/版本/授权、项目状态、笔记/经验/决策及派生检索索引。
- Knowledge 原始字节、整份抽取文字、完整语义块正文与上传暂存使用共享 `FileStore`。原 `raw/text` 列保存 `tianshu-knowledge-file/v1` 描述；块 `payload` 保存跨度及 `text_file`。API 仍返回原始字节/文字与原引用，原件 SHA256、版本和坐标不变。
- 分块文件化不拆散原完整证据组。FTS 只负责派生检索；取结果时恢复完整块，并继续验证项目根、文档版本/hash、来源新鲜性和实时授权。
- 文件先通过临时文件、fsync、原子发布落盘，再提交 SQL 引用。失败可能留下未引用文件，不会提交半文件引用。不会自动清理文件；SQL 中过期上传字节引用仍按原一小时规则清空，文件库的空间/保留管理需考虑这些可回收对象，不能按内容哈希盲删仍被其他版本引用的文件。
- 源项目目录保持注册根和当前只读访问边界。迁移不写、移动或删除这些原始目录。

## SQLite 接续与 PostgreSQL

SQLite 旧 Knowledge 行在 Store 启动时按一次独占事务文件化。变更前保留同目录唯一 `.pre-knowledge-files-<uuid>.bak`，schema 3 同时保存对应 `.source-guard.json`。业务文档/块/授权版本与引用不变，来源检查点正常推进。新建 Knowledge schema 直接写文件引用，不在普通目录阅读时反复执行迁移。

首次 PG 接续从公共包提供的只读 SQLite 快照导入。产品逐表核对转换后完整行，包括历史、序号、幂等结果、suppression 和消费水位；文件引用逐个恢复并验证。自动递增序列按导入后的水位推进。原 SQLite 保留，后续仅目标 PG 写入。旧结构的显式 `migrate-*` 命令仍按自己的前置版本执行，不把缺失归属推测成可信来源。

`postgres_schema.sql` 是本产品原生 DDL。JSON 操作使用 PG JSONB 表达式；普通索引、GIN 全文索引、身份序列、来源 revision 触发器及临时授权语料均在本域维护。Memory 对当前授权完整候选建立会话私有临时语料，按 BM25 排序；Knowledge/经验/笔记用原生全文相关性排序。两者的精确数值分数不保证与 SQLite FTS5 相同，但范围、完整组、版本、撤销和预算约束保持。

## 独立检查点、备份与恢复

SQLite 保留 `BEGIN IMMEDIATE + FULL`。PG 在同一个业务事务中取得本 schema 的事务 advisory lock，并启用同步提交；所有来源权威写者都必须经过 Store。拿锁后先验证独立 guard，事实变更后先 fsync guard 再提交 PG。提交中断可使 guard 领先数据库，此时服务继续拒绝，不能从旧库重建 guard 为 ready。该锁不使用 Redis，也不代表已允许跨主机任意多写部署。

首次空 PG 目标在本地状态目录独立预留 authority identity，绑定本次缓存/安装代次；首个提交中断后可沿同一 identity 重试并校验已写的零代 guard，绝不覆盖或猜测历史 guard。已活跃数据的恢复仍遵循独立检查点规则。

显式迁移在 SQLite 产生原 SQLite backup；PG 迁移的同名备份路径保存 `tianshu-memory-postgresql-backup/v1` 一致性逻辑 JSON（列、完整行、独立 guard 描述），不会打开或复制已落后的 SQLite。它是迁移恢复证据，不能用 SQLite 工具直接打开。

部署备份必须同时包含：当前 SQL 一致性备份、原件/块/全文文件根、独立 source-guard、`<database_path>.storage/`、配置和原凭据/合同挂载。共享 PG 的角色/schema 与备份恢复由协调部署流程保存；不可只备数据库后声称原件可恢复。不要同时回滚 SQL 和独立 guard 来绕过旧恢复检查；本卡没有新增来源恢复批准后门或 PG→SQLite 自动回切。

## 验证

沿用项目完整测试。真实 PG 专项必须显式设置 `TIANSHU_TEST_POSTGRES_URL` 指向隔离合成库，然后运行：

```text
python -m pytest tests/test_storage_backends.py -q
```

未设置时 PG 项明确跳过，不能算 PG 通过。用例为每次测试生成 `ts232<uuid>` schema 并在完成后删除自己的 schema，不对真实资料/模型/QQ 执行请求。涵盖真实 TLS 合成来源、双 actor 抑制隔离、消费/批次回执、整组召回、关系冻结/预算、旧结构迁移、文件化、上传授权、角色 sidecar、并发 guard、重启、旧恢复拒绝与首迁中断重试。
