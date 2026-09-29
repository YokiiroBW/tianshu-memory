# QQ-IDENTITY-20260929 · Memory

- 目标：沿用现有 QQ 账号到 `person_id` 的正式注册链，保存来源范围内的昵称和群名片，给后台提供只读独立档案视图；同名异号绝不合并。
- 基线：`0ff79ed315a4d707fd1dd42152e41f28bf7ad2b4`；协调者后续多角色候选 `870d9269d33b88ed34c6445eb7d95f791b43a914` 仅合同文档增量，待本任务提交后对齐。交付提交为包含本文件的本地 `codex/qq-identity-20260929` HEAD。
- 变更：QQ 账号 ID 严格校验；仅 Platform caller 可用的 `/internal/v1/identity/qq-alias`、`qq-profiles`；别名幂等、来源机器人/群范围、无账号不创建、页面名称回退；显式 schema 3 别名迁移包含独立备份、`source_revision` 触发器，不随启动自动运行。
- 合同：`docs/contracts-candidates/qq-identity/v1` 为候选，含请求、响应及事件 v2 样例；平台独立 person 归属保持不变，未来关联只预留联合读位置。
- 实际验证：Memory HTTP 测试覆盖 Platform 与 Companion 凭据隔离、同名异号、同号跨 BOT、分页、重启、事件重复及冲突、无账号/非法 QQ/恶意显示文本/错误时间戳。原身份鉴权、HTTPS 授权、档案查询、来源迁移和观察测试受影响范围已通过。
- 部署前配置：停写并备份，执行 `migrate-qq-aliases --backup <全新本地路径>`，确认 schema 3/source guard；分别配置 `platform_qq_alias` 仅有 `qq_alias`、`platform_qq_profiles` 仅有 `qq_profiles` 的不同凭据，供 Platform `qq_alias_memory`、`web_qq_profiles` HTTPS 连接。旧账号/档案不合并、不批量改名。
- 未完成/风险：未对后续多角色文档候选重基；无真实数据迁移、生产凭据、QQ 或 NAS 操作。浏览器档案 peer 是合成 HTTPS 服务；本工作树验证的是 Memory 实际 HTTP 接口。下一步协调者按固定提交审查、发布合同、准备受控迁移与回滚。
