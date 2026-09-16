# 项目接续包与工作目录事实核对（TS-083）

接续包回答的是“**现在**在哪个检出上继续、项目声明了什么、下一步是什么”，并把三件永远不能混为一谈的东西分开呈现：仓库历史、本服务已索引的摘要、当前工作目录的实际内容。只读操作复用既有 `KnowledgeApplication`、`recover`/`check`/`write_state` 语义、目录增量索引用到的索引表与 `FileReader`/`file_path` 规则；不新增表、不新增迁移、不建第二个项目数据库、不写任何项目文件。

## 配置

```json
{
  "knowledge": {
    "projects": {"demo": {"root": "C:/isolated/demo", "host": "local", "default_branch": "main", "urls": []}},
    "worktrees": {
      "demo": [
        {
          "id": "hermes-a",
          "path": "C:/isolated/demo-hermes",
          "branch": "main",
          "clients": ["hermes-demo"],
          "files": ["docs/design.md", "src/service.py"],
          "max_bytes": 262144
        }
      ]
    },
    "clients": {
      "hermes-demo": {
        "credential_sha256": "<SHA256 of an independent secret of at least 16 characters>",
        "projects": ["demo"],
        "permissions": ["query", "recover", "check", "write_state", "continuation_recover", "continuation_check"]
      }
    }
  }
}
```

- `worktrees.<project>` 是**唯一**的工作目录登记；没有该段就完全不能接续，目录不从项目根推断、不从调用参数放宽。
- 每条登记：`id`（`[A-Za-z0-9][A-Za-z0-9._-]{0,63}`，同一项目内唯一）、`path`（绝对路径、无 `..`、任意层级不得是 credentials/secret/token/runtime/models/build 等目录；允许位于点目录之下）、`branch`（期望分支）、`clients`（哪些 client 能使用该检出）、`files`（最多 16 个相对项目根的受限文件，名称规则与单文件导入一致：拒绝隐藏名、凭据/密钥/运行目录、不支持扩展名）、`max_bytes`（1 KiB–8 MiB，约束登记文件摘要的读取总量）。
- 未登记或属于其他 client 的 `id` 一律 `worktree_unregistered`（403），不区分“不存在”和“不属于你”，因此不会泄露他人是否登记了检出。
- Git 可执行文件按操作者环境解析：环境变量 `TIANSHU_GIT`（完整路径或可被 `PATH` 解析的名字），否则 `PATH` 上的 `git`；两者都没有时明确 `git_unavailable`（503），不回退、不伪成功。

## 显式操作

```powershell
uv run python -m tianshu_memory.knowledge_cli --config C:/private/memory.json action --client hermes-demo --credential-env TIANSHU_PROJECT_SECRET C:/private/continue.json
```

```json
{"operation":"continuation_recover","project_id":"demo","arguments":{"worktree":"hermes-a","text":"receipt retry","budget_bytes":16384}}
{"operation":"continuation_check","project_id":"demo","arguments":{"package":{...}}}
```

两个操作都是只读，权限名与操作名一致并分别授予；包的 `seal` 绑定 client 凭据摘要，另一个 client 无法校验他人的包。已初始化与未初始化的项目都可以接续：未初始化项目的 `state` 为 `null`、`index` 为空、`revision` 为 0，且**只读预览不创建项目行**（与目录预览一致）。缺少读权限、项目未登记、凭据不符仍然是 `forbidden`/`unauthorized`/`project_unregistered`。

## 包的事实分层

`continuation_recover` 返回（上限 32768 字节预算，最小 4096）：

| 段 | 内容 | 是否本次实测 |
| --- | --- | --- |
| `worktree` | `id`、期望分支与实际分支、是否 detached、`head`、是否 unborn、脏状态分类计数、登记文件摘要、Git 版本/超时/输出上限/允许的子命令 | 是，本次只读采集 |
| `history` | 最多 8 条提交（`commit`/`subject`/`committed_at`），达到上限时 `complete=false` | 是，但只到上限为止 |
| `index` | 本服务已索引文档的 `document_id`/版本/状态/存储哈希/`freshness` | 否；仅登记摘要与返回的资料单元被核验 |
| `state` | `goal`、`constraints`、`unfinished`（下一步）、`recent_verification`、`pitfalls`、`evidence` | 引用被本次核验 |
| `units` | 完整资料单元（`reference`/`spans`/`text`/`source_id`/`provenance`，从不切半） | 引用被本次核验 |

- `index.documents[].freshness` 只有三种：`verified_current`（登记摘要与索引哈希一致）、`verified_changed`（登记摘要与索引哈希不一致，或当前文件缺失/不可读/超限）、`unverified`（未登记摘要，本服务不声称它是当前文件）。**旧摘要永远不覆盖当前文件**，两者并排展示。
- `worktree.files[]` 给当前摘要、大小与状态（`present`/`missing`/`unreadable`/`changed`/`too_large`）以及同一 locator 的索引行；读取中变化或超限是诚实的状态，不是假成功。登记路径若变成越界链接、凭据名或不支持类型，则整次接续失败关闭（`workdir_file_refused`/`workdir_file_unsupported`）。
- 脏状态只给**分类计数**（`staged`/`modified`/`deleted`/`renamed`/`untracked`/`conflicted`/`entries`），不列脏文件路径、不读 diff、不读未跟踪文件正文。采集结果与检出自己的 `git status` 一致（专项用例对照断言）。
- 包不回声宿主绝对路径，也不含任何凭据。

## 采集器边界（可信本地只读）

固定、可枚举的只读 Git 调用，全部带 `--no-optional-locks`（不刷新、不写索引）与 `-c core.fsmonitor=false`（不运行仓库配置的 helper，专项用例用真实 fsmonitor 钩子证明）：

| 调用 | 用途 | 上限 |
| --- | --- | --- |
| `rev-parse --show-toplevel` | 登记路径必须是自己检出的根（子目录、非仓库分别 `workdir_not_root`/`workdir_not_repository`） | 1024 B |
| `rev-parse --verify --quiet HEAD^{commit}` | HEAD；未出生分支为 `null` + `unborn=true` | 128 B |
| `symbolic-ref --short -q HEAD` | 分支；detached 时 `branch=null` + `detached=true` | 1024 B |
| `status --porcelain=v1 -z --untracked-files=normal --ignore-submodules=all` | 分类计数 | 256 KiB，超出即 `workdir_output_too_large`，不静默截断 |
| `log -n 8 --no-show-signature --no-decorate --format=...` | 历史 | 64 KiB |
| `--version` | 可追溯的 Git 版本 | 256 B |

- 没有 `fetch`/`checkout`/`reset`/`clean`/`diff`/`pull`，不联网、不执行仓库脚本、不进入子模块；专项用例断言实际调用的子命令集合是允许列表的子集、索引 mtime 不变、远端跟踪引用在远端前进后仍不动（证明不 fetch）。
- 每次调用固定 10 秒超时；HEAD 在 `status` 前后各读一次，采集期间检出移动即 `workdir_changed`，绝不报告半新半旧。
- 提交标题等仓库文本按不可信数据处理：去控制字符、按 200 字符截断。
- 整个采集发生在两个短事务**之间**，慢 Git/磁盘 I/O 不持共享写锁（并发用例：采集被挂起时真实 `MemoryService.register`/`resolve` 仍在 1 秒内完成）。
- 采集不写入工作目录：不改文件、不改索引，`git.writes` 恒为 `never`。

## 验证绑定与失效

`write_state` 的 `recent_verification` 允许两种形状：

```json
{"summary":"targeted suite passed","worktree":"hermes-a","commit":null}
"legacy sentence without a commit"
```

结构化条目由服务端在写入时盖上**实际观测到**的 `commit`/`branch`/`dirty`/`declared_at`；调用方声明一个并非当前 HEAD 的 commit 会被 `workdir_conflict`（409）拒绝，且此时不写入任何状态。一个状态最多绑定 4 个不同检出（`too_many_worktrees`）。读取时按本次实测重新标注 `scope`：

| scope | 含义 |
| --- | --- |
| `current` | 同一检出、同一 HEAD、声明时与现在的脏状态一致 |
| `historical_commit` | HEAD 已前进，这是历史提交上的结果 |
| `workdir_changed` | HEAD 相同但脏状态变了（例如声明后出现/消失了未提交改动） |
| `other_worktree` | 绑定的是另一个已登记检出，本包不做跨检出判断 |
| `unbound` | 纯字符串或未出生分支：**永远**不冒充当前提交的测试 |

`continuation_check` 原样回传包，服务端重新观测该检出并给出 `valid`/`reason`/`differences`/`observed`。命中顺序固定：

| reason | 触发 |
| --- | --- |
| `stale_or_tampered` | seal、project_id 或项目登记指纹不符（含被改动的包） |
| `registration_changed` | 工作目录登记本身变了（路径、分支、clients、files、上限） |
| `revision_changed` | 项目 revision 变了（导入/删除/状态写回） |
| `index_changed` | 包的索引段与当前索引行不符 |
| `branch_changed` | 切分支或进入/离开 detached |
| `head_changed` | 新提交或 HEAD 回退 |
| `workdir_dirty` | 出现/消失未提交改动（分类计数变化） |
| `file_changed` | 登记文件的摘要/大小/状态变化 |
| `stale_evidence` | 包内证据引用不再是当前来源 |

工作目录整个不可观测时返回 `observed=false` 的裁决（`reason` 为 `workdir_unavailable`/`not_repository`/`timeout` 等），而不是静默成功；`continuation_recover` 在同样条件下失败关闭。撤权、移出项目、凭据错误在任何读取之前就 `forbidden`/`unauthorized`，未迁移的 schema 3 库仍是 `dependency_unavailable`。

## 字节预算

`budget.stdout` 从来不是估计值：`budget.used_bytes` 等于返回包的 UTF-8 JSON 实际字节数（用例断言相等）。可选资料单元按预算装入，装不下时记 `omissions=["budget"]`；**必读部分**（工作目录事实、历史、索引、已声明状态）从不切分，装不下时以 `budget.over_budget=true` 明示并仍返回完整状态。没有 tokenizer，因此 `tokenizer=null`、`token_counts=unavailable`，不伪报 token。

## MCP

官方 SDK stdio 工具为 `knowledge_continuation_recover` 与 `knowledge_continuation_check`（只读标注），共 22 个工具：

```powershell
uv run --extra mcp python -m tianshu_memory.knowledge_cli --config C:/private/memory.json mcp --client hermes-demo --credential-env TIANSHU_PROJECT_SECRET
```

不监听文件系统、不轮询、不自动登记、不写任何编码体配置，也不向外部模型发送代码。

## 验证命令

```powershell
$env:TIANSHU_GIT = 'C:/path/to/git.exe'   # PATH 上已有 git 时可不设
uv run --extra mcp pytest tests/test_knowledge.py tests/test_knowledge_transport.py tests/test_knowledge_concurrency.py tests/test_knowledge_directories.py tests/test_knowledge_continuation.py -q --basetemp .runtime/tests-ts083-targeted
uv run --extra mcp pytest -q --basetemp .runtime/tests-ts083-full
```

## 限制

1. 两台机器/两个编码体各自登记自己的检出即可分别接续同一项目；本服务不做跨检出比较，`other_worktree` 的验证只标注归属，不判断对方的 HEAD。
2. `index` 段中未登记摘要的文档永远是 `unverified`：本服务只核验返回的资料单元与登记摘要，不重读整个索引（有界读取优先，无大库吞吐承诺）。
3. 历史最多 8 条、登记文件最多 16 个、脏状态最多 256 KiB 输出；超限明确报错或标注 `complete=false`，不静默截断。
4. 分支名、`path` 与 `files` 都属于登记内容，登记变化会让旧包与旧绑定失效或冲突，需要重新计算。
5. 仍是本地 SQLite 单写者；未操作任何真实账号、群消息、设备、NAS 或生产数据库。
