# 天枢记忆

TS-080 项目资料、显式写回、短恢复包与可选 MCP stdio 入口见 [项目知识运行说明](docs/project-knowledge.md)。独立项目域不写人物画像；默认不启用 MCP 或自动配置任何客户端。

TS-081 在其上追加有版本的项目错题本（触发/症状/原因/修正/验证/适用范围 + 当前来源证据）与**显式批准**的总经验：至少两个不同项目的有效错题证据、独立 `promote`/`review` 权限、来源撤权或修订后立即不可复用，只返回调用者授权的内容。见 [项目错题本与总经验](docs/project-lessons.md)。

TS-083 追加项目接续包：对一个显式登记的独立工作目录只读核对 Git 仓库/分支/HEAD/脏状态与少量登记文件摘要，把仓库历史、已索引摘要与当前未提交事实分开呈现，并把“最近验证”绑定到实际观测到的提交；`continuation_check` 在切分支、新提交、新未提交改动或索引变化后立即判为失效。见 [项目接续包与工作目录事实核对](docs/project-continuation.md)。

Python 人物与记忆服务，绑定已发布 text-dialogue/v1、profile-memory/v1 与 source-sync/v1 **1.0.0**。提供 SQLite/WAL、完整语义组预算检索、画像查询、修订/遗忘与候选幂等账本。TS-033 增加角色来源账本、物理否定传播、双 owner 读取屏障与真实 HTTPS 来源客户端。

`source_sync` 模式经配置的 Core/Platform HTTPS 接口核验来源；`local_fixture` 保留隔离合成演示。实际测试包含合成 owner 经真实 HTTPS、Memory HTTP 与 SQLite 重启，不代表真实 Core/Platform 产品联合通过。TS-034 提供独立部署凭据认证的本地用户确认、画像批准/发布/撤销入口；未配置时仍不可用。Chat Audit、跨平台账号证明、PostgreSQL 与嵌入检索仍未接入。候选受理不自动生成记忆。

本地用户入口、精确权限登记与批准迁移见 [本地用户操作](docs/local-user-actions.md)。

正式来源配置、schema 3 迁移、独立恢复检查点和容量边界见 [来源同步运行说明](docs/source-sync-runtime.md)。旧演示脚本继续使用 schema 1/2；不要用 fixture-action 向 schema 3 登记来源或批准。

## 安装与验证

Python 3.12+，TS-033 实际使用 CPython 3.12.14。所有命令在本仓库任务检出运行：

```powershell
uv sync --locked --group dev
uv run ruff check .
uv run ruff format --check .
uv run python -m compileall -q src tests scripts
uv run pytest tests/test_identity_auth.py tests/test_recall.py tests/test_recall_regressions.py tests/test_revisions_events.py -q
uv run pytest tests/test_source_transport.py tests/test_source_migration.py tests/test_trusted_workflow.py tests/test_source_sync.py -q
uv run pytest -q
```

测试优先读取 `TIANSHU_CONTRACT_DIRECTORY`；否则读取当前检出的 `.runtime/workspace-context.json` 定位主工作区已发布合同。独立克隆需显式设置：

```powershell
$env:TIANSHU_CONTRACT_DIRECTORY = 'C:/YOKI/Codex/tianshu-peiban-bot/contracts/text-dialogue/v1'
```

项目接续的只读工作目录核对需要外部 Git 可执行文件：先看 `TIANSHU_GIT`，再看 `PATH` 上的 `git`，两者都没有时明确返回 `git_unavailable`（不回退、不伪成功）：

```powershell
$env:TIANSHU_GIT = 'C:/path/to/git.exe'   # 仅当 PATH 上没有 git 时需要
```

运行时读取并核验发布目录 manifest 与其中所有摘要，不复制共享 schema，也不读取旧候选合同。本服务自己的测试会将合同形状的请求送进实际端点；主工作区 `contracts/validate.py` 不是这里的产品测试。

## 启动隔离演示

```powershell
uv run python scripts/create_local_fixture.py --contracts $env:TIANSHU_CONTRACT_DIRECTORY --output .runtime/demo
uv run tianshu-memory --config .runtime/demo/config.json serve --port 8130
```

用生成的合成数据运行 18 个标注检索样本：`uv run python scripts/evaluate_fixture.py --fixture .runtime/demo`。报告在该目录 `evaluation.json`，包含明确的合成表述与长度，不含真实原文或身份 ID。评估使用隔离数据库副本，包含常见字干扰、多主题近邻和仅够一组的预算；这只是精确/关键词样本，不是通用语义检索成绩。

初始化脚本只创建合成账号、来源与经显式审核的结构化记忆，并生成演示请求与私有令牌。拒绝覆盖已有配置；服务只监听 `127.0.0.1`，退出终端即可关闭。没有 QQ/TG、模型、生产库或设备连接。HTTP `/health` 明确显示 `local_fixture` 与未接入能力。

脚本生成的 `select-private.json`、`select-group.json` 可通过 HTTP POST 调用；服务 Bearer 令牌见生成的本地 `config.json`。运行接口、配置和应用层工作流见 [运行说明](docs/runtime.md)。预算与正确性边界见 [实现决定](docs/design.md)，当前交付见 [TS-030 交接](docs/handoffs/TS-030.md)。
