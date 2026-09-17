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
| `worktree` | `id`、期望分支与实际分支、是否 detached、`head`、是否 unborn、脏状态分类计数、未提交状态指纹、登记文件摘要、Git 版本/超时/输出上限/允许的子命令与已中和的 helper 名 | 是，本次只读采集 |
| `history` | 最多 8 条提交（`commit`/`subject`/`committed_at`），达到上限时 `complete=false` | 是，但只到上限为止 |
| `index` | 本服务已索引文档的 `document_id`/版本/状态/存储哈希/`freshness` | 否；仅登记摘要与返回的资料单元被核验 |
| `state` | `goal`、`constraints`、`unfinished`（下一步）、`recent_verification`、`pitfalls`、`evidence` | 引用被本次核验 |
| `units` | 完整资料单元（`reference`/`spans`/`text`/`source_id`/`provenance`，从不切半） | 引用被本次核验 |

- `index.documents[].freshness` 只有三种：`verified_current`（登记摘要与索引哈希一致）、`verified_changed`（登记摘要与索引哈希不一致，或当前文件缺失/不可读/超限）、`unverified`（未登记摘要，本服务不声称它是当前文件）。**旧摘要永远不覆盖当前文件**，两者并排展示。
- `worktree.files[]` 给当前摘要、大小与状态（`present`/`missing`/`unreadable`/`changed`/`too_large`）以及同一 locator 的索引行；读取中变化或超限是诚实的状态，不是假成功。登记路径若变成越界链接、凭据名或不支持类型，则整次接续失败关闭（`workdir_file_refused`/`workdir_file_unsupported`）。`max_bytes` 是**整组登记文件**的累计读取预算：每个文件的预计大小在打开它之前就计入，装不下时 `workdir_byte_budget`（409），最后一个文件也不会让读取总量超过配置值。
- 脏状态给**分类计数**（`staged`/`modified`/`deleted`/`renamed`/`untracked`/`conflicted`/`entries`）与一个未提交状态指纹 `worktree.changes`：`mode=stat_only`、`entries`（Git 报出的改动条目数）、`complete`（是否全部改动都被完整描述）、`fingerprint`（对每条改动的状态码、**路径的 SHA256**、大小、mtime、mode 排序后取 SHA256）。Git 会把整个未跟踪目录折叠成一条 `?? drafts/` 记录，而**目录自身的 stat 不是它内部文件的版本**（改写已有的 `drafts/new.py` 不会移动目录 mtime），所以折叠目录按内部文件**逐个**描述：按名称排序、深度与条目数受同一上限约束，目录里出现 `.git`（嵌套检出，它的状态在它自己的仓库里）、junction、不可读目录、超出上限，或该目录已消失/变成链接/不再可进入时，整份描述标 `complete=false`——**无法描述就不判有效**，绝不用目录 stat 冒充子文件版本。指纹只读元数据：不列脏文件路径、不读 diff、不读任何文件正文（未跟踪正文尤其不读，也不计入登记文件的读取预算）。计数相同不代表状态相同，因此 `check` 比对的是指纹而不是计数（专项用例：同计数二次改写、折叠目录内改写、目录 mtime 变化、目录 stat 冒充版本四种情形必须区分开）。
- 采集结果与检出自己的 `git status` 一致（专项用例对照断言）。
- 包不回声宿主绝对路径，也不含任何凭据；脏文件路径只以摘要形式参与指纹。

## 采集器边界（可信本地只读）

“只读”是对调用的性质要求，不是对意图的声明：仓库可以让 Git 在**读取**时执行程序（`.gitattributes` 选中的 `filter` clean/process driver、`diff` textconv 或外部命令、文件系统监视器）。因此采集前先从合并配置中列出全部 `filter`/`diff` driver 名，并在每次调用的命令行上用更高优先级的空值覆盖它们；子进程还使用固定环境（不是服务环境），任何继承的 `GIT_*`（`GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_*`/`GIT_EXTERNAL_DIFF`/`GIT_DIR`/`GIT_ASKPASS` 等）都无法定义可执行程序。宿主的 Git 配置本身**保留**（否则 `core.autocrlf`/filter 设置会让刚提交的检出错判为已修改），被中和的 driver 名会出现在包的 `worktree.git.helpers` 里，便于核对。

driver 名是**整段 subsection**，允许含点：`filter.probe.dot.clean` 属于 `probe.dot` 而不是 `probe`，因此按末尾的**完整变量名**（`clean`/`smudge`/`process`/`required`/`clean.required`/`smudge.required`、`diff` 的 `command`/`textconv`）从最长形式起解析，绝不按第一个点截断——截断会把真正的 driver 留着运行却记成“已中和”。**无法安全表达的名字明确拒绝**：`-c` 键在第一个 `=` 处分割，所以含 `=` 的 driver 名无法被无歧义覆盖（覆盖会落到别的键上，真 driver 仍在运行），控制字符与超长名同理；`filter` 段没有普通变量，其中本服务不认识的变量也无法证明无害。以上情况一律 `workdir_helper_unrepresentable`（503；`check` 侧是 `observed=false` 的裁决），而不是猜一个名字再声称已中和。

固定、可枚举的只读 Git 调用，全部带 `--no-optional-locks`（不刷新、不写索引）、`core.fsmonitor=false`、`core.hooksPath=<不存在的目录>`（不运行钩子）、`status.submoduleSummary=false`、`log.showSignature=false`，以及每个已发现 driver 的 `filter.<name>.{clean,process,smudge,required}`/`diff.<name>.{command,textconv}` 覆盖：

| 调用 | 用途 | 上限 |
| --- | --- | --- |
| `config --null --list` | 列出配置里的 driver 名以便中和（只读配置，不执行任何东西；名字用代码过滤、按完整变量名解析，避免正则参数被 `cmd` 垫片改写或含点 driver 名被截断；无法安全覆盖的名字拒绝为 `workdir_helper_unrepresentable`） | 64 KiB |
| `rev-parse --show-toplevel` | 登记路径必须是自己检出的根（子目录、非仓库分别 `workdir_not_root`/`workdir_not_repository`） | 1024 B |
| `rev-parse --verify --quiet HEAD^{commit}` | HEAD；未出生分支为 `null` + `unborn=true` | 128 B |
| `symbolic-ref --short -q HEAD` | 分支；detached 时 `branch=null` + `detached=true` | 1024 B |
| `status --porcelain=v1 -z --untracked-files=normal --ignore-submodules=all` | 分类计数与未提交状态指纹 | 256 KiB，超出即 `workdir_output_too_large`，不静默截断 |
| `log -n 8 --no-show-signature --no-decorate --format=...` | 历史 | 64 KiB |
| `--version` | 可追溯的 Git 版本 | 256 B |

- 没有 `fetch`/`checkout`/`reset`/`clean`/`diff`/`pull`，不联网、不执行仓库脚本（专项用例用真实 clean/process driver——含含点的 `filter=probe.dot` 名字与无法覆盖的 `filter=a=b` 名字——与真实 fsmonitor 钩子证明：对照调用确实执行了仓库程序，采集后标记文件不存在或整次采集明确拒绝）、不进入子模块；专项用例断言实际调用的子命令集合是允许列表的子集、索引 mtime 不变、远端跟踪引用在远端前进后仍不动（证明不 fetch）。
- 输出上限是**采集期硬上限**：stdout 与 stderr 分别有界，任何一个越界就杀掉子进程并报 `workdir_output_too_large`（不是先无限缓冲、事后才发现）；每次调用固定 10 秒超时，到点同样杀进程并报 `workdir_timeout`。stderr 内容从不返回。
- HEAD 在 `status` 前后各读一次，采集期间检出移动即 `workdir_changed`，绝不报告半新半旧。
- 提交标题等仓库文本按不可信数据处理：去控制字符、按 200 字符截断。
- 整个采集发生在两个短事务**之间**，慢 Git/磁盘 I/O 不持共享写锁（并发用例：采集被挂起时真实 `MemoryService.register`/`resolve` 仍在 1 秒内完成）。
- 采集不写入工作目录：不改文件、不改索引，`git.writes` 恒为 `never`。

## 验证绑定与失效

`write_state` 的 `recent_verification` 允许两种形状：

```json
{"summary":"targeted suite passed","worktree":"hermes-a","commit":null}
"legacy sentence without a commit"
```

结构化条目由服务端在写入时盖上**实际观测到**的 `commit`/`branch`/`dirty`/`declared_at`，并附上 `facts`——该次观测整份可观测状态的指纹（HEAD、分支、脏标记、分类计数、未提交状态指纹、登记文件摘要）。因此写入绑定也会读取登记文件（受同一累计字节预算约束，读不下即 `workdir_byte_budget`）。调用方声明一个并非当前 HEAD 的 commit 会被 `workdir_conflict`（409）拒绝，且此时不写入任何状态；检出状态无法被完整描述（改动条目超过上限，或折叠的未跟踪目录无法完整描述）时绑定被拒绝为 `workdir_unproven`（409），因为这样的记录日后无法比对。一个状态最多绑定 4 个不同检出（`too_many_worktrees`）。读取时按本次实测重新标注 `scope`：

| scope | 含义 |
| --- | --- |
| `current` | 同一检出、同一 HEAD，且本次指纹与写入时的 `facts` 完全一致（干净检出在无指纹的旧记录下也可证明：内容就是该提交） |
| `historical_commit` | HEAD 已前进，这是历史提交上的结果 |
| `workdir_changed` | HEAD 相同但状态指纹不同（声明后再次改写未提交文件、出现/消失改动、登记文件摘要变化） |
| `workdir_unproven` | 无法证明相同：旧记录没有指纹而检出是脏的，或改动条目超过上限无法完整描述。**不冒充 current** |
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
| `workdir_unproven` | 计数与脏标记未变但未提交状态指纹不同，或包本身的状态描述不完整（改动条目超上限、折叠的未跟踪目录无法完整描述）：无法证明相同即不判有效 |
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
3. 历史最多 8 条、登记文件最多 16 个、未提交状态最多描述 512 条改动（同一个上限也约束折叠未跟踪目录内部的文件，超限时 `complete=false`，`check` 与验证绑定按“无法证明”处理）、单次 Git 输出上限 256 KiB；超限明确报错或标注，不静默截断。未提交状态指纹只读元数据（大小/mtime/mode）与路径摘要，不读内容：若某个文件被改写后大小、mtime、mode 与路径全都保持不变，指纹无法察觉——这是“不读未跟踪正文、不做无界 diff”这一边界的已知代价，专项用例覆盖的是同尺寸改写（mtime 变化）、不同尺寸改写（大小变化）与折叠目录内改写三类现实情形；登记文件的摘要仍是内容摘要，不受此限。内含 `.git` 的未跟踪目录（嵌套检出）、junction 与不可读目录不做深挖，一律标 `complete=false`（因此该状态下验证绑定会被拒绝），这是有意的失败关闭而不是漏报。
4. 分支名、`path` 与 `files` 都属于登记内容，登记变化会让旧包与旧绑定失效或冲突，需要重新计算。
5. 仍是本地 SQLite 单写者；未操作任何真实账号、群消息、设备、NAS 或生产数据库。
