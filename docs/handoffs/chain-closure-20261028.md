# Memory PostgreSQL 分段原始事件闭环

运行基线：`6ae7a1adfd8fb2ed5ee7d92a3ee5819e230678f9`。运行候选：`8d01f9d9d39fba33b6ca8dca9d2599730c9c7e1e`，分支 `codex/chain-closure-20261028`。本交接为后续独立文档提交，不改变运行候选。

## 已确认的失败与归属

父级在 R12 实际 Linux wheel 上运行完整原生合成陪伴/日程工具。SQLite 路径已完成；PG 路径通过六服务真实后端绑定、SDK 身份读取、同账号两私聊 processing 与正常模型回复后，Memory 原材料入口连续十二次返回 `dependency_unavailable`。这十二次请求用时约 100–229 ms，原 Companion source-facts 与 Platform source-access 回调均为 200。60 秒是工具等待回执的期限，不是这十二次请求的网络超时。

独立隔离 PG17 日志明确记录：`UPDATE turn_inputs SET event=...` 失败，`column "event" does not exist`。PG 静态建表缺少该列；显式 segment migration 有补列，但完整 SQLite 源已带 `memory_segment_schema=1`，PG 首次导入直接复制标记而不重跑显式迁移。源 `turn_inputs` 为空时激活成功，首次真实原材料写入才失败；非空且含 event 列的源会更早在导入阶段失败。故障属于产品 PG schema 映射，不能在验收夹具中 ALTER、删除标记或伪造 guard 来掩盖。

父级只读确认线上 Memory 本来已有 event 列，Knowledge 未启用 segment 且没有此列。该线上观察由父级取得；本执行者没有 SSH、生产读写或部署动作。本候选不要求生产修复 DDL、数据恢复或迁移。

## 运行变更

唯一四个运行文件：

- `src/tianshu_memory/postgres_schema.sql`：PG 新建 turn_inputs 包含 nullable `event TEXT`，确保空表、非空原始事件导入均完整。文件 SHA256：`513fa2432934174479fad5a35d128b35921553158fbe076132dd58ef33a902f8`。
- `src/tianshu_memory/segment_migration.py`：统一拥有 segment 标记与 event 列的兼容谓词；原显式迁移兼容已激活 PG 的这一个缺列情况。
- `src/tianshu_memory/app.py`：Memory 业务装配只读核验兼容性，失败即关闭已打开的 Store 并拒绝启动业务。
- `src/tianshu_memory/runtime_probes.py`：原 database 探针在原只读连接中复用同一兼容谓词，缺列报告 failed；原 deadline、错误规则不变。

Store 仍可作为维护入口打开结构缺列的 PG，避免普通启动拒绝后原 CLI 也无路可修。未启用 segment 的旧 Knowledge/Memory 不要求 event 列。普通业务请求、装配和探针均不补列。

## 显式修复边界

仅对已启用 segment、PG 缺少 event 列的库，冷停业务写者后可使用原命令：

```sh
python -B -m tianshu_memory.cli --config /explicit/private/config.json \
  migrate-segments --backup /explicit/new/missing-event.backup
```

原 Store 事务及 PG advisory lock 先核验独立 source guard；迁移仍要求 schema 3、consolidation 1、observation 1，先独占创建当前后端的完整逻辑备份及 guard 副本，再只执行 `ALTER TABLE turn_inputs ADD COLUMN event TEXT`。不重跑 install，不改 marker、source_revision 或触发器，不填充任何历史 event。历史原文不能从缺失列恢复时继续为 NULL，不能声称已补齐原文。成功结果包含 `repaired_event_column=true`。列已完整时继续返回原 `already_installed=true`，不创建/覆盖备份。

既有备份冲突、guard 失配、SQLite 的同类结构损坏均不进入该 PG 窄修。该修复不是恢复批准，不能用于重建丢失权威表、撤销记录或 guard。

## 实际本地验证

使用既有共享 Python，仅以 PYTHONPATH 选择此 checkout 的 Memory src 与原 pytest vendor；合同显式指向冻结的 `verification-r3/memory-fixture/contracts/text-dialogue/v1`。没有安装或修改解释器内包，没有连接 PG。

- Ruff check、format check 与 `git diff --check` 通过。
- `tests/test_runtime_probes.py tests/test_segment_io.py`：25 passed / 29 skipped。29 项跳过来自原 diagnostics package 发现路径不可达，不计作已验证。
- 新增 `tests/test_segment_io.py::test_missing_segment_journal_is_not_ready_or_repaired_by_business_startup` 与原 `tests/test_segments.py::test_schema6_needs_explicit_migration_and_consume_stops_creating_new_singletons`：4 passed，覆盖 private/group。实际 SQLite 结构缺列后探针失败、业务装配拒绝、guard 保持，普通启动不补列；原显式新迁移仍可用。
- 下述 PG 四个 base node 收集得到 11 个参数节点；这里只完成 collect，没有执行或计为 PG 通过。

## 父级实际 Linux wheel / PG 验证命令

挂载本提交的完整 tests 目录（文件间复用原 fixture），不挂 src；必须使用实际安装的 Memory 新 wheel。工作目录例如 `/validation/memory`，仅 `.runtime` 可写。需要冻结完整 Memory 合同与原有含 cryptography 的 TLS tooling Python。

```sh
export PYTHONDONTWRITEBYTECODE=1
export TIANSHU_CONTRACT_DIRECTORY=/verification/memory-fixture/contracts/text-dialogue/v1
export TIANSHU_TEST_POSTGRES_URL='postgresql://chain_test@localhost/chain_test?host=/tmp/chain-pg'
export TIANSHU_TEST_CERT_PYTHON=/explicit/existing/tooling/python
python -B -c 'import os; assert os.environ["TIANSHU_TEST_POSTGRES_URL"]'
python -B -m pytest -q \
  tests/test_storage_backends.py::test_postgres_segment_import_preserves_original_journal \
  tests/test_storage_backends.py::test_postgres_segment_missing_journal_refuses_business_and_repairs_only_with_backup \
  tests/test_storage_backends.py::test_postgres_segment_journal_repair_keeps_existing_backup_and_guard \
  tests/test_storage_backends.py::test_postgres_unsegmented_authority_accepts_legacy_journal_without_event \
  --basetemp /validation/memory/.runtime/chain-closure-pg
```

DSN 必须由父级替换为专属隔离 PG17 的实际值。没有新增 `MEMORY_REQUIRE_POSTGRES` 开关；原 fixture 无 DSN 会 skip，故调度器必须先断言环境变量存在。原 `postgres_store` 已断言实际 backend 为 PostgreSQL，不能把 fallback SQLite 算作 PG 通过。

11 个节点覆盖：空/非空真实 schema6 导入后的原始事件、材料与重复回执（4）；active 缺列业务/探针拒绝，原 CLI 独占备份补列，元数据/guard/触发器/既有行保持与幂等（2）；已有备份和错误 guard 阻止补列（4）；未启用 segment 的 Knowledge 旧结构保持可用（1）。

同一实际新 wheel 还需重跑父级保留的完整原生 `phase2-native-schedule.py` PG 路径。该工具仍冻结为 SHA256 `3dc196a83094cb3a6cbe421856beabdcd4f6fcc5145d5f846f3b98eeb7c55db7`，本次未修改或加入异常 instrumentation。R12 SQLite 的成功结果不能充当本候选的 PG 完整闭环结果。真实 QQ、付费模型、网页登录、crash 恢复与生产迁移均不在本交付验证声明内。
