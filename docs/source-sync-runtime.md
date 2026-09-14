# source-sync/v1 运行接线

TS-033 基线 Memory `69b29f3a6b8cd61d39d733870d136de2caeb75f0`，正式包来自协调根发布提交 `154f068`。固定 source-sync/v1 1.0.0 manifest 的 UTF-8 LF SHA256 为 `178d0ce66210bdfad4cfb85d8b5f0905b0b67f834e2a530efe5636ff0373633d`。加载时校验包内全部文件及固定 text/profile 依赖；不读取历史 candidate，不修改 wire/hash。

## 服务配置

配置文件和数据库均使用显式本地路径，放在被忽略的 `.runtime/`。生产来源模式为 `source_sync`，其新增配置结构如下；示例主机是占位，不能作为已部署地址：

```json
{
  "mode": "source_sync",
  "database_path": "C:/isolated-memory/memory.sqlite",
  "contract_directory": "C:/workspace/contracts/text-dialogue/v1",
  "source_sync": {
    "recovery_path": "C:/independently-retained-memory/source-guard.json",
    "core": {
      "url": "https://core.example.invalid/internal/v1/source-facts/read",
      "token": "SET_DISTINCT_MEMORY_TO_CORE_CREDENTIAL",
      "ca_file": "C:/isolated-memory/ca.pem"
    },
    "platform": {
      "url": "https://platform.example.invalid/internal/v1/source-access/read",
      "token": "SET_DISTINCT_MEMORY_TO_PLATFORM_CREDENTIAL",
      "ca_file": "C:/isolated-memory/ca.pem"
    }
  },
  "callers": {
    "companion": {
      "token": "SET_DISTINCT_CORE_TO_MEMORY_CREDENTIAL",
      "issuer": "platform",
      "issuer_url": "https://platform.example.invalid/internal/v1/origins/resolve",
      "issuer_token": "SET_MEMORY_ORIGIN_CREDENTIAL",
      "issuer_ca_file": "C:/isolated-memory/ca.pem",
      "allowed_actors": ["actor:a"],
      "operations": ["resolve", "register", "select", "select_profiles", "consume", "revise", "check_sources"],
      "event_scopes": []
    }
  }
}
```

`event_scopes` 是既有后台接口允许的精确 scope 列表，需配置实际映射后的 actor/person/audience/conversation；空列表拒绝 consume/check。后台读取不依赖过期 origin，但必须通过 Memory 来访认证、Core 持久事实和 Platform 当前底层权限。Platform 的 source current 凭据需绑定 `principal.kind=service, service=memory, action=source.current`；它与在线 viewer 的 `companion→memory` origin 不同。每次出站重新读凭据/配置以支持撤销。URL/CA/token 不接受请求体输入，必须 HTTPS，证书验证不可关闭，不跟随跳转，不继承环境代理。私有 CA 路径可省略以使用系统默认信任。

新 HTTP 接口只有正式 `POST /internal/v1/memory/source-sync/check`，请求/响应为 `shared#check_request/check_response`。入口操作配置名是 `check_sources`。其他 text/profile 路径与信封不变；严格 JSON 拒绝重复键和 NaN。

本地演示可按旧 README 的 loopback HTTP 启动。需要直接 TLS 的隔离服务可用现有 uvicorn：

```powershell
$env:TIANSHU_MEMORY_CONFIG = 'C:/isolated-memory/config.json'
uv run python -m uvicorn tianshu_memory.app:configured_app --factory --host 127.0.0.1 --port 8130 --ssl-certfile C:/isolated-memory/server.pem --ssl-keyfile C:/isolated-memory/server.key --no-access-log
```

也可由部署的固定 HTTPS 入口终止 TLS；本任务未部署代理或生产服务。`/health` 仅报告组件已配置，不能当远端健康/联合 L0 证明。

## 同步与账本

`sources` 的 key 在 schema 3 为 A=`{key:{channel,message_id},actor_id}` 的规范摘要，lineage/source_writes/jobs/profile approvals 同步迁移。`physical_sources` 独立保留 P 当前事实/墓碑；`source_admissions` 保存真实受理事实，`suppression` 为 A 跨 revision 否定。`owner_heads` 保存两个 owner 的 generation/sequence，`source_observations` 保存逐 P、A 受理、turn、A 当前授权在上次观察水位的内容，拒绝同水位异事实。

每次 select/profiles（包括零预算）、consume、候选提交、revise、check：

1. 本地事务取得 m0 和完整 scope coverage，立即释放锁。
2. 固定 HTTPS Core C1 → Platform P1（在线重验 viewer）→ Core C2。Core head 改变最多三次重试。
3. 新本地事务比较 m0/coverage；按本地账号绑定核对 person/binding_version，应用 P/A 及所有派生失效、版本、outbox、双水位，然后提交。
4. 独立业务事务再比较本地 revision/权限/known version。事务间本地变更重新同步；409 不回滚已提交失效。

SQLite 写锁不跨网络。数据库触发器记录依赖变化，跨进程写入同样推进 m0。text coverage 包括本范围保留的历史来源；profile coverage 包括当前 actor 全部活动公开/当前群血缘，不按目标、作者、query 或预算裁剪。P 否定影响所有已知本地 actor 的血缘；只对当前授权 A 正向登记。失效同域一次推进，已失效投影后续私密活动不推进公开 epoch。A correct/forget 只抑制本 A，不删除其他 A 或物理原文。

`SourceAuthority.sync` 对应 `memory.sync` 可信应用端口：只有进程内 HTTPS 编排产生 observation，m0 是网络前捕获的本地执行状态。没有任意 observation HTTP 导入接口。已校验发布包 rules 只作关系断言；认证、当前时钟、本地 binding 与事务是产品实现。

committed_event 比较 owner 持久事件除 event_id 外的全部字段、turn/input revision、scope/conversation、有序 input_sources 和 aggregate 水位；当前 P/A、回执、归档状态及分类同时匹配。允许经 owner 证明的 aggregate 跳号；拒绝同 P 双修订、回执别名、伪 reality、mixed/unclassified 单来源和 archived 来源。

## 可信写入

`TrustedWorkflow.commit_candidate({job_id,drafts})` 使用正式 workflow schema；后台 worker 必须是进程内受信调用者，且草稿精确等于来源事件 scope。候选受理不调用模型，不自动生成草稿；写入、关系累计和 A+revision+scope 去重在一个事务中。拒绝整批超出 256 个返回 record 的草稿，不部分提交。

`TrustedWorkflow.confirm_revision(input)` 保留正式 confirmation_input，但真实批准通过 TS-034 的 `LocalUserApplication`/`user-action` 进入；只接受其具体认证适配器，任意 verify_approval 返回 True 不再有效。无配置为 503。身份来自平台实时 resolve 与部署独立用户凭据，精确绑定 semantic_request、account、scope、binding_version、record/expected_version 和期限，一次消费，同 HTTP 幂等重试不重置 consumed。HTTP 没有签发确认入口。

画像 approve/publish/revoke 复用完整草稿和 SQL 业务规则，增加明确主体/类别/群或公开范围以及来源/绑定/部署权限快照。需显式 `migrate-users --backup`，配置和操作见 [本地用户操作](local-user-actions.md)。fixture 来源/批准不进入真实批准链。无界全库索引重建仍为 503。

correct 只禁用旧值，返回 corrected/invalidated/pending；replacement 不进入可读新组，不承诺自动补全。新的可信 replacement 来源及原子新组由后续任务完成。

## 迁移、备份、异常和恢复

停止全部本库写入进程，使用同一运行配置。新库从 schema 1 开始；schema 1 先迁移 profile，再迁移 source。已是 schema 2 只运行第二步：

```powershell
uv run tianshu-memory --config C:/isolated-memory/config.json migrate-profiles --backup C:/isolated-memory/backups/schema1.sqlite
uv run tianshu-memory --config C:/isolated-memory/config.json migrate-sources --backup C:/isolated-memory/backups/schema2.sqlite
```

备份必须是新的、不同于数据库的本地文件，不覆盖已有文件。SQLite backup API 在阻止其他写入时取得完整已提交快照（含 WAL 内容）。迁移同一事务检查普通 lineage 的完整 scope、画像的已消费批准与共享域、source_writes/job 的已证明范围，再重写 A 键及相关快照。混 actor/person/群私/会话、来源键错误或未证明画像批准会回滚整个迁移，保留 schema 2 和备份，供后续审查；不按 ID 前缀猜归属。

旧来源迁移为 `verified=0`，没有伪造 Core admission/物理 receipt/owner head。旧 withdrawn 只生成已证明 A 的保守 suppression，不伪造 P 墓碑。旧确认没有 binding_version，不能在生产 revise 中使用。首次正式屏障验证后才可读取仍有效的旧组。

schema 3 迁移同时一次性建立独立恢复检查点，含库实例 UUID、本地 revision 与恢复状态。可显式用 `source_sync.recovery_path` 放到独立保留位置；省略时派生为 `<database_path>.source-guard.json`，它是独立文件，**不代表独立故障域**。只有来源/授权/版本/确认/候选/幂等账本/outbox 等权威状态改变才推进检查点，稳定读取和无关 conflicts 日志不推进；观察到 owner 水位前进仍须持久保存。每个相关 Store 写事务在 SQLite 提交前持久化检查点。正常重启不重置；缺失、坏 JSON、数据库旧备份回退、错配实例，或检查点先写后数据库提交中断，均失败关闭。不能删除检查点让服务重新初始化，也不能从旧库重建它。

这是**回退检测与拒绝**，不会恢复已经丢失的 suppression/消费账本。如果数据库和检查点同时回退到匹配的旧版本，单机无法检测；测试明确记录该不支持的恢复方式，不把它当成功恢复证明。Windows 上先写新的临时文件、flush/fsync、关闭句柄，再以同目录 `os.replace` 替换检查点；旧读句柄均及时关闭。检查点成功但数据库 commit 失败可能使检查点领先，从而牺牲可用性并保留拒绝状态。

本地日志分别给出 `source_checkpoint_missing`、`source_checkpoint_unreadable`、`source_checkpoint_mismatch` 或 `source_checkpoint_commit_interrupted`；HTTP 仍沿正式错误合同返回不可用，不扩充错误码或泄露数据。维护者必须停止所有写入，保留数据库、WAL、最新检查点与备份，核对各权威账本的完整性；没有完整证据就继续隔离。本版没有人工恢复批准/解锁实现，不可通过覆盖检查点绕过。

迁移失败且尚无任何 schema 3 业务写入时，可以继续保留/使用原 schema 2 及旧模式，或在全部服务停止后由维护者审查完整备份。schema 3 已接受业务后，不支持降级或自动恢复：独立保留当前检查点及完整最新数据库/WAL、suppression、消费/写入 ledger、outbox、P 墓碑和双水位；恢复必须另行核对这些记录。只恢复旧 SQLite 或同时回退所有独立副本无法证明恢复完整性，本任务不提供恢复批准、重建或解锁命令。不要覆盖最新检查点以让旧快照变为 ready。

## 容量及验证边界

单个完整 coverage 最多 256 个 A，P 去重；Core 请求最多 32 个 turn（本实现每次业务最多一个）；单响应/出站请求最多 1 MiB。Memory 入站保留原 256 KiB 限额。超限/缺项/超时/双水位回退/generation 改变返回不可用，绝不截断、漏墓碑或拼接不同快照分页。长期累积超限会持续 503；稳定分页、增量恢复另审。

测试区分：真实 loopback HTTPS 上的正式合成 owner、实际 Memory 认证与 SQLite 事务；真实 Memory HTTP 子进程与重启；隔离 SQLite 迁移/恢复故障；替换 operation 的工作流局部测试。它们都不是已发布 Core/Platform 的联合验收。真实账号、设备、模型、渠道发送、归档和 PostgreSQL 未操作，完整 L0 保持未通过。Core 稳定区间内 P1 是共同读点，返回/外部发送仍可与后续远端变更竞争，不声称分布式发送事务。
