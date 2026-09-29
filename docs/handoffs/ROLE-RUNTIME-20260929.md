# 多角色运行时交接：Memory

## 目标、基线与提交

- 任务：`C:/YOKI/Codex/tianshu-peiban-bot/docs/development/role-runtime-plan-2026-09-29.md`。
- 基线：`10214b2f7dbfb6990e986f38f92c8103e07d6b9e`。
- 固定提交：本文件所在提交（SHA 随总控交接消息提供）；未合并、未推送、未部署。

## 变更

- 新增独立 SQLite actor grant 账本，按精确 actor 持久化版本/启停/操作回执。只有已登记 `platform` caller 且 `role_admin:true` 可访问管理端点；仅显式配置 `allow_runtime_roles:true` 的 Companion caller 可消费动态 grant，其他来源仍需原静态授权。
- 每次 origin 解析继续核验完整 actor/account/channel/person scope，动态名单不替代这些检查。后台 consume/source check 在原精确 scope 与来源授权之外核对动态授权。停用 grant 阻止新的 Memory 请求，不删除历史记录。
- 显式接管静态 actor 时 `legacy:true` 必须与部署静态 allowlist 一致；停用后的动态拒绝覆盖旧静态 allowlist，旧启用请求重放不会解除新拒绝。未接管静态角色保持原行为。
- 候选合同 schema/实例见 `docs/contracts-candidates/role-runtime/v1/`，与另外两产品任务检出同版，未发布根合同。

## 实际验证

- `tests/test_role_grants.py`：5 通过，覆盖合同实例、幂等/版本冲突/重启/撤销、静态角色明确接管与旧启用回放拒绝、重启认证器后旧静态 allowlist 仍拒绝、同 person/conversation 的原有 A/动态 B 双向 Memory 越界请求 403。
- 身份认证、召回及画像边界相关 21 项通过（两项依赖库弃用警告）；Ruff 通过。
- Platform 联合 HTTPS 测试真实调用本产品的 grant、origin/Memory 路径，1 通过；仅隔离合成账号/来源/模型，不含生产记录。

## 部署与恢复准备

- `role_grants_database_path` 必须是持久存储上的绝对路径，独立于主 Memory DB。部署配置为 Platform caller 设置独立 token 与 `role_admin:true`；为 Companion caller 设置 `allow_runtime_roles:true`，并保留原 `allowed_actors`、`event_scopes`、issuer/origin 服务配置。不得给其他 caller 通配许可。
- 维护窗口内成组备份主 Memory DB、grant sidecar、Platform 主 DB+role sidecar/provider catalog 与 Companion DB。初次创建空 sidecar 不扩大旧数据可见性；若需回滚，必须成组恢复，避免恢复旧静态授权而遗漏新版停用状态。
- 不自动接管 household 或其他静态角色；接管动作由 Platform 提供 exact actor 和 legacy 标记。

## 未完成/风险与下一步

- Platform 失败矩阵 `docs/handoffs/ROLE-RUNTIME-FAILURE-MATRIX.md` 列出 12 项覆盖及缺口。同 person/conversation 跨 actor 的双向 Memory 探测、原有静态角色停用后重启拒绝和 group/public 画像边界已验证。所有动态角色与画像状态组合及全阶段停用竞态未穷举。
- 总控需审查合同与三个固定提交，并在隔离环境演练迁移/恢复后再决定生产启用。
