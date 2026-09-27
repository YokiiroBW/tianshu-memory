# CONNECT-M 项目经验与交接只读 HTTP 接入（候选）

沿用已存在的 Knowledge 独立进程、TLS/Host/Origin/诊断与 `/local/v1/project-knowledge/action`。本轮只把四个既有领域**读**操作加入受限 HTTP；启动时固定 `--client`，每请求 Bearer 由该固定 `knowledge.clients.<client>` 验证。Platform 只能服务端调用，不把凭据、原始包或内部路径交给浏览器。

## 双层授权与部署

在 Memory 私有配置中为 Platform 浏览连接器登记**独立** knowledge client；服务端配置至少含 `credential_sha256`、`projects`、逐操作 `permissions` 和新增 `http_read_operations`。后者仅可列四个新增 HTTP 操作；未写或空列表时它们一律 415 `unsupported`，旧十二操作行为保持。领域仍逐请求验证凭据/项目/同名 operation 权限；`experience_query` 还必须有独立 `review` 权限，`lesson_query` 的 `read` 不授予经验审阅。`continuation_*` 还只能访问服务端 `knowledge.worktrees` 已登记且允许此 client 的 checkout 别名。禁用、删权限、移项目或改凭据按领域原规则实时失败关闭，HTTP 额外授权在返回前再次读取，撤销后的结果不交付。

结构示意（值必须由总控在私有部署中正式发放，不能原样运行）：

```json
{
  "knowledge": {
    "clients": {
      "platform-project-reader": {
        "credential_sha256": "独立随机Bearer的SHA256十六进制摘要",
        "projects": ["已登记项目ID"],
        "permissions": ["lesson_query", "experience_query", "review", "continuation_recover", "continuation_check"],
        "http_read_operations": ["lesson_query", "experience_query", "continuation_recover", "continuation_check"]
      }
    }
  }
}
```

实际还需原 Knowledge 项目、数据库、工作树/来源与部署日志配置；服务端响应不表示迁移已完成。若用户只需错题检索，不授 `experience_query`/`review`；若没有已登记 checkout，不授 continuation。Platform 应按当前登录用户到项目的正式权限映射选择**固定** client 和项目，不接受浏览器自报 client、项目或路径扩大权限。不能造通用 `operation` 代理。

## 精确 HTTP 请求

固定 `POST /local/v1/project-knowledge/action`，`Content-Type: application/json`，`Authorization: Bearer <独立知识客户端凭据>`。正文只能有 `operation`、`project_id`、`arguments`：

```json
{"operation":"lesson_query","project_id":"已登记项目ID","arguments":{"text":"receipt","budget_bytes":8192}}
{"operation":"experience_query","project_id":"已登记项目ID","arguments":{"text":"receipt","budget_bytes":8192,"project_id":null}}
{"operation":"continuation_recover","project_id":"已登记项目ID","arguments":{"worktree":"已登记checkout别名","text":"receipt","budget_bytes":16384}}
{"operation":"continuation_check","project_id":"已登记项目ID","arguments":{"package":{"...":"仅此前 recover 返回的完整包"}}}
```

`lesson_query`/`experience_query` 是**文本检索**，分别返回 `lessons`/`entries` 和 `omissions`，不是项目全量目录；最小预算 256、最大 32768 字节。Platform 可投影有界标题、摘要、当前引用及省略状态给页面。`experience_query` 的内部 `arguments.project_id` 可空；不跨出登记 `projects`，并且每条经验所有证据项目都要有权限。当前来源失效、撤销或不再符合权限时领域检索不返回旧条目。

`continuation_recover` 的 `worktree` 是服务端登记的**别名**，不是绝对路径、任意 Git 命令或新仓库。它在明示的“读取交接”动作中观察该 checkout，只读 Git/已登记文件与当前项目状态，返回客户端绑定且可由 `continuation_check` 验证的封装快照；预算 4096–32768 字节。**不持久化包、不修改 checkout、不调用导入/scan/apply/write_state**，但会执行有界文件和 Git 读取；不要页面后台轮询。包内 `revision`、索引 freshness、声明验证 scope 与 `checked_at`/`observed`/`differences` 应明确展示历史或过期，不能称“当前始终有效”。Platform 仅向网页返回必要安全投影，隐藏完整 seal、内部定位与本机路径；内部完整包留在受信服务端用于 `continuation_check`。

成功直接沿用 `KnowledgeApplication.execute` 结果及其预算/来源/版本字段，`Cache-Control: no-store`。未授权新 HTTP 操作 415 `unsupported`；Bearer、项目、独立 review/同名权限、未登记 worktree 等领域拒绝沿用 422 和领域原码；超时、过载、Host/Origin 拒绝沿用既有 transport 错误。旧十二操作及其配置完全不变；不开放 `experience_promote`、lesson 写、`write_state`、`directory_scan/apply` 或迁移。

本任务只在隔离夹具及本地真实进程验收；真实 Platform project 映射、NAS 配置/数据与浏览器链由总控/B/U 分别验收。
