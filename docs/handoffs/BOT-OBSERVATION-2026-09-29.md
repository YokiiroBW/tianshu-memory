# 机器人账号观察 · Memory 交接

## 目标与基线

- 基线 `02df5ba8c041a7a6eb8b705c5f264e10543032ec`；分支 `codex/bot-observation-policy-20260929`，本交接随实现提交固定。
- 保留被动观察正文与来源证明，不进入聊天轮次、候选记忆、工作流或模型请求。

## 变更

- 显式 `migrate-observations --backup NEW_PATH` 为既有 schema 3 增加独立 `observation_sources` 表与 revision 触发器；不自动迁移、目标备份不得覆盖。
- `ObservationLedger` 经 Platform HTTPS verifier 实时核实来源，按 `source_ref`/digest 幂等存储；按实例、机器人账号、会话和当前 `archive_epoch` 查询正文。历史撤销返回稳定 `scope_changed` 409；临时依赖故障保持可重试。
- 仅配置了 `observation_source` 且 caller 含 `observe_ingest`/`observe_query` 时，v2 ingest/query 可用；caller 是 Companion，不是浏览器或普通聊天调用方。查询投影经 Companion 返回 Platform 管理界面。

## 实际验证

- Ruff 通过；`TIANSHU_CONTRACT_DIRECTORY` 指向已发布 `text-dialogue/v1` 时 `tests/test_observations.py` 2/2，覆盖迁移、幂等、隔离、分页、暂停和撤销。
- 三产品真实 TLS/HTTP 联合测试通过，使用实际 `configured_app` 从完整私有配置初始化，验证 Platform verifier 认证、故障恢复、503 重试与 409 撤销；无候选记忆、job 或 turn 输入副作用。

## 生产配置与迁移顺序

1. 停止 Memory 写入进程并确认数据库/WAL 静止；保留既有备份和恢复步骤。新备份必须是尚不存在的本机路径。
2. 在私有 config JSON 中保留现有 `database_path`、`contract_directory` 等字段，增加 `callers.companion.operations` 中的 `observe_ingest` 与 `observe_query`，并增加 `observation_source`：`verify_url` 为 Platform HTTPS `/internal/v2/observation-source/verify`，`verify_token` 为 Platform Memory reader 凭据，`ca_file` 为其信任根绝对路径。Platform 对该 principal 授予 `observation.verify`。
3. 运行 `tianshu-memory --config PRIVATE_CONFIG migrate-observations --backup NEW_LOCAL_BACKUP`。确认返回 `schema:3, observation_schema:1`，且原 `observation_sources` 新表中 `conversation` 列保持不变；然后启动 `serve`，确认新的 v2 路由在 TLS 下对未授权调用返回 401。
4. 再启动 Companion/Platform，先只观察一个合成账号，检查同账号同群可读、其他账号和私聊不可串读。生产数据迁移、部署和真实账号接入由协调者执行。

## 未完成与风险

- 未在生产 `/srv/tianshu/memory.sqlite` 运行迁移或连接真实数据。候选合同尚未冻结且运行时不加载其 JSON Schema；本实现使用各端严格字段校验与联合样例验证。
- 历史读取撤销通过授权和 epoch 隔离，不物理删除原 SQLite 行；如需合规擦除，需要独立审批和迁移设计。Platform/Companion 失败时查询不会绕过 verifier。

## 引用

- 根候选合同 `contracts/observation-source/candidate-v1`；Platform 与 Companion 同名交接文件。
