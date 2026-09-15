# 项目资料、目录增量导入与短恢复包（TS-080/TS-082）

这是独立项目知识域，使用同一 SQLite Store 和 schema 3 source-guard；不生成聊天 P/A、Core receipt、SourceAuthority 证明或人物画像。项目资料不依赖远端聊天来源核验；所有操作经 KnowledgeApplication 的配置授权与 Store 事务。没有模型调用、自动整理、全局踩坑晋升或项目文件写入。TS-082 增加登记目录的“扫描预览→显式确认→持久增量导入”，仍复用同一 UTF-8 格式、版本与 FTS5 索引，不新增解析器或嵌入后端。

## 安装、迁移与配置

基础 CLI 使用现有依赖；可选标准 MCP 使用官方 SDK 维护分支 `mcp>=1.28,<2`，当前锁定 1.30.0。`uv sync --locked --group dev --extra mcp` 安装可选组件。选择 v1 是为了使用已文档化的 FastMCP stdio API；不引入第二协议框架。[官方 SDK v1 文档](https://py.sdk.modelcontextprotocol.io/v1/)。

停写、备份后执行：

```powershell
uv run python -m tianshu_memory.knowledge_cli --config C:/private/memory.json migrate --backup C:/private/before-knowledge.sqlite
```

已安装的 TS-080/TS-081 数据库单独补目录计划表（同样先停写、显式备份）：

```powershell
uv run python -m tianshu_memory.knowledge_cli --config C:/private/memory.json migrate-directories --backup C:/private/before-directories.sqlite
```

需要已显式迁移到 schema 3 的 Memory 数据库。迁移保留聊天历史，新增表均带 source_revision 触发器（含 `knowledge_plans`）。备份路径必须不存在。失败关闭检查点不允许通过覆盖 guard 或单独还原旧 DB“修复”；恢复需另行审查。迁移前备份不含新增表，迁移后的 guard 会拒绝该旧库。不要用生产数据库试跑。未执行目录迁移的库对目录操作返回 `dependency_unavailable`（503），不会退化成别的写路径。

在私有 Memory JSON 中添加下列配置；不是可直接用于生产的凭据。`root`、`host`、`default_branch` 和显式 URL 列表组成固定登记，首次成功写操作入库；只读调用不能隐式登记空项目。登记变化失败关闭，目录迁移另行审查；禁止自动猜测和扫描项目。`directories` 是可选的目录扫描登记：没有该段就完全不能扫描，目录只能由操作者显式列出，不从项目根推断。

```json
{
  "knowledge": {
    "projects": {
      "demo": {
        "root": "C:/isolated/demo", "host": "local", "default_branch": "main",
        "urls": ["https://example.com/design"]
      }
    },
    "directories": {
      "demo": [
        {"path": "docs", "max_files": 64, "max_bytes": 4194304}
      ]
    },
    "clients": {
      "hermes-demo": {
        "credential_sha256": "<SHA256 of an independent secret of at least 16 characters>",
        "projects": ["demo"],
        "permissions": ["query", "recover", "check", "write_state", "import", "delete", "status", "directory_scan", "directory_apply"]
      }
    }
  }
}
```

只读客户端仅登记 query/recover/check；`directory_scan` 是只读预览权限，`directory_apply` 是写入权限，两者必须分别授予。`path` 是相对项目根的已登记子目录（不能是根本身、隐藏目录、`.git`、凭据/密钥/运行/模型目录或经链接越界的路径），`max_files` 为 1–256，`max_bytes` 为 1 KiB–8 MiB，且都属于登记内容，调用方不能自行提高；单项字节上限沿用来源读取的 1 MiB。凭据通过指定环境变量传入，启动参数绑定 client；每次工具调用重新读取配置、核验摘要和 project/operation 权限。请求体不得声明身份。配置文件必须只有可信操作者可写；同操作系统账号完全控制配置和进程的人属于本地可信边界，不提供多租户 OS 隔离。

## 显式操作

执行完整 JSON 文件意味着操作者授权该操作。所有路径使用绝对路径：

```powershell
uv run python -m tianshu_memory.knowledge_cli --config C:/private/memory.json action --client hermes-demo --credential-env TIANSHU_PROJECT_SECRET C:/private/import.json
```

导入示例：

```json
{"operation":"import","project_id":"demo","arguments":{"key":"design-import-1","kind":"file","locator":"docs/design.md","expected_version":0,"groups":null}}
```

`kind=url` 时 locator 必须精确匹配已登记 URL。新来源 expected_version=0；后续替换必须提供当前版本。成功、内容未变和可预期读取失败均持久记录；status 通过原 key 查询历史结果。相同客户端/key 同负载返回 replayed，表示历史执行结果，**不表示来源仍当前**；变更负载拒绝。失败后的重试使用新 key。删除是逻辑 tombstone 并增加版本；原始资产文件始终只读，历史 raw 版本保留供审查，并非物理擦除 API。

默认完整来源为一个原文块。初版不假称通用语义抽取：人工确认更小语义块后，groups 可给覆盖全部行、不重叠的 `{start,end,depends_on}`；行号从 1 开始、闭区间，depends_on 引用从 0 开始的组索引。依赖闭包不可拆分，不截断条件/否定/因果。过大完整块预算不足时遗漏，不切半。Markdown/代码不运行，UTF-8 文本保真；HTML 保存原字节及去 script/style/template 的可见文本，引用定位明确为可见文本行而非 HTML 原字节行。

支持 txt/Markdown、所列代码扩展和 HTML；PDF、Office、图片、数据库、二进制和非 UTF-8 明确不支持。仅单文件导入，无递归扫描；目录外、隐藏目录、运行目录、模型目录及凭据名称等拒绝。允许目录必须由操作者选择为非秘密资料目录，文件名规则不是内容 DLP。文件查询/恢复会重读 hash，发现未提交变化时旧块和关联状态不再返回；不自动覆盖、提交或修改工作树。

URL 只支持 HTTPS 公网 443，逐跳精确允许列表、最多 3 次重定向、1 MiB、15 秒网络总期限、identity 编码及 text/plain/markdown/html。DNS 解析采用系统解析器，其 OS 超时额外计入；TLS 连接固定到校验过的公网 IP 并保留主机名证书检查，不继承代理、不抓取链接、不执行脚本。URL 查询只代表最近显式导入快照，不在后台联网检查远端变化。URL 刷新失败立即使旧版本不可召回。导入先在短事务捕获项目状态，再在事务外抓取、解析、hash 和构建索引文本，提交前重读授权和登记，原子复核项目修订、文档版本与幂等结果。查询/恢复/写回证据也在事务外读取文件，之后短事务复核一致性；文件读取前后核验文件标识/大小/时间，双次内容校验不一致时拒绝旧证据。并发项目变更返回 project_conflict，不自动覆盖或无限重试；无关聊天写入不使项目操作冲突。OS DNS 阻塞及取消均不占用数据库写锁。仍无大库吞吐承诺。

查询：`{"operation":"query","project_id":"demo","arguments":{"text":"receipt retry","budget_bytes":8192}}`。复用 FTS5、NFKC/英文词与中文双字词检索，不是向量或 AI 语义搜索。只返回命中完整块、版本/hash、source_id、来源和行范围；预算为实际 UTF-8 JSON 字节（256–32768）。至多考察 128 个候选，没有按全项目全文装配提示词。

项目写回使用 `write_state`，参数为 key、expected_version、state。state 精确包含 goal、constraints、recent_verification、unfinished、evidence、pitfalls。前三个列表与 unfinished 是明确操作者陈述；evidence 必须使用 query 返回的当前 reference（block_id/document_id/version/hash）。pitfalls 默认为项目域，项包含 trigger/symptom/cause/correction/verification/evidence。没有自动全局共享或模型生成假成功。

`recover` 与 query 参数相同，预算 1024–32768；当前状态和全部证据整体放入，装不下就显式 state_budget，失效则 stale_state。包带项目 revision、登记摘要和 seal。每次缓存复用前执行 `check`，参数 `{"package": <完整原包>}`；替换、删除、项目状态更新、配置变化、当前文件变化或内容篡改均不能冒充当前有效包。无法撤回已被外部客户端复制的文本，客户端必须执行 check。

## 登记目录增量导入（TS-082）

`directory_scan` 预览一个已登记目录，`directory_apply` 确认该预览。预览只写计划本身（`knowledge_plans`，source_revision 已跟踪），不创建项目行、不导入任何文件：

```json
{"operation":"directory_scan","project_id":"demo","arguments":{"directory":"docs"}}
```

预览为每个候选固定路径、类型、大小、摘要和当前索引版本，并返回 `plan_id`（覆盖计划全部字段的指纹）、`omitted`（拒绝的路径与原因：不支持类型、名称拒绝、单项过大、非 UTF-8、空文件、读取中变化、超文件/字节预算）、`missing`（已索引但当前不可扫描的来源，含原因）和 `counts`/`walk_truncated`/`budget_incomplete`。目录树按名称排序、深度优先，只列登记目录内**相对项目根**的 POSIX 形式 locator；符号链接与 junction 一律不下钻也不列出，被拒绝的目录不下钻也不列名，被拒绝的文件只列路径与原因。单项（1 MiB）与登记的文件数/总字节只约束本次真正要写入的项，已是最新的文件照常列出但不占预算，因此反复预览仍能继续推进。走查上限 2048 条目；一旦截断或预算用尽，本次**不产生任何 missing 候选**——漏扫和超预算永远不能当删除。

确认必须原样回传该预览（`key`、`plan`、`tombstones`），服务端校验：计划形状与自洽指纹、计划项身份与 locator 推导一致（`source_id`/`document_id` 不能指向别的文档）、计划属于该 client/项目/目录、计划仍是该范围当前发出的预览（重新预览会使旧计划 `plan_superseded`）、项目登记与目录登记未变（登记收紧后旧计划的 limits 超限同样冲突）。随后在事务外重读每个文件并逐项复核路径/类型/大小/摘要/当前版本；任一项变化只让该项成为该次结果的 conflict，绝不写入与预览不同的字节：

```json
{"operation":"directory_apply","project_id":"demo","arguments":{"key":"dir-apply-1","plan":{...},"tombstones":[]}}
```

- `source_changed`/`source_missing`/`source_too_large` 等：该项不导入，`status` 为 `partial`，`remaining` 列出待处理路径，`rescan` 为真——先重新预览再确认。
- `version_conflict`：扫描后被并发手工导入改过内容（或已删除），旧计划不会回滚该版本，也不会用旧内容覆盖。
- 相同内容已是最新（含上次取消后已提交的项）记录为 `unchanged`，不重复写版本，因此取消/重启后可继续。
- 每项一个短事务；项目行只在首次真正写入时创建。慢文件读取不持共享写锁，长任务在每次写入前重读配置授权，撤权或登记变化立即失败关闭。

删除策略是 `explicit_approval_only`：`missing` 只是候选，只有把该 `document_id` 明确列进 `tombstones` 才会写索引墓碑（版本+1、移出索引、项目 revision+1）；路径又出现时记 `present_again` 且不删除，未列出的记 `not_approved`。本服务任何路径都不会写入、移动或删除项目文件。结果（含逐项 outcome、conflicts、counts、remaining）持久记录在 `knowledge_operations` 与 `knowledge_imports`，可用原 key 通过 `status` 读回；同请求重放返回 `replayed` 的历史结果。该结果的失效沿用既有语义：目录导入会增加 revision 与来源版本，因此旧 `recover` 包、`lesson_check` 与依赖这些来源的 `experience_check` 立即不再有效。

## MCP 与 Hermes

MCP stdio 入口：

```powershell
uv run --extra mcp python -m tianshu_memory.knowledge_cli --config C:/private/memory.json mcp --client hermes-demo --credential-env TIANSHU_PROJECT_SECRET
```

二十个独立工具：knowledge_query、knowledge_recover、knowledge_check、knowledge_import_status、knowledge_import、knowledge_write_state、knowledge_delete、knowledge_directory_scan、knowledge_directory_apply，以及 TS-081 的 lesson_record、lesson_revise、lesson_retire、lesson_query、lesson_recover、lesson_check、experience_promote、experience_query、experience_check、experience_revoke、experience_withdraw。读写工具分开且服务端逐次验证权限；annotations 仅为客户端提示，不是授权。目录工具同样只做一次显式预览/确认，不监听文件系统、不轮询、不自动全盘扫描。SDK 负责 initialize/tools/list/tools/call 和 stdio framing，stdout 只用于协议。默认未启用、不监听 HTTP、不修改任何编码体全局配置。

Hermes 可作为标准 MCP stdio host，显式登记 command/args/env；示例见 `integrations/hermes/README.md`。协议契约仍为本任务 docs/candidates 下候选，未宣称跨产品冻结或 Hermes 已安装/已接通。标准 SDK 客户端测试与真实 Hermes 账号/客户端验收分开记录。

## 验证命令

`uv run --extra mcp pytest tests/test_knowledge.py tests/test_knowledge_transport.py tests/test_knowledge_concurrency.py tests/test_knowledge_directories.py -q --basetemp .runtime/tests-ts082-targeted`；全产品回归 `uv run --extra mcp pytest -q --basetemp .runtime/tests-ts082`。TLS 工具解释器配置沿用 docs/runtime.md。变更稳定后先审查完整 diff，再跑 ruff/compileall 和对应测试。运行结果见任务交接。
