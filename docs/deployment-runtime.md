# 部署运行时：双 HTTP 服务、只读探针与安全运行日志

本文件是 TS-102 交付的运行时说明，覆盖两个独立 HTTP 进程（聊天 `memory`、项目知识
`memory-knowledge`）的启动方式、绑定与 TLS 规则、两个只读探针的语义、运行日志的容量与故障
语义，以及容器镜像的构建与运行方式。**本文件描述的是本地已实现并已用隔离测试验证的部分；
它不声称任何生产环境已经通过验收。**

相关文档：`docs/runtime.md`（本地运行与隔离环境变量）、`docs/project-knowledge-http.md`
（受限知识 HTTP 入口自身的边界）、`docs/source-sync-runtime.md`（source-guard 与迁移纪律）、
`docs/handoffs/TS-102.md`（本卡交付、未完成项与验证证据）。

---

## 1. 两个服务的职责边界

| 服务 | `service` 字段 | 入口 | 身份来源 |
| --- | --- | --- | --- |
| 聊天 | `memory` | `tianshu-memory --config <文件> serve` | 配置文件内的既有身份与来源装配 |
| 项目知识 | `memory-knowledge` | `python -m tianshu_memory.knowledge_cli --config <文件> --client <身份> serve` | 每次请求的 `Authorization: Bearer` |

两个服务是**两个进程**，各自持有自己的日志 sink、自己的 `instance_id`、自己的 `sequence`
计数和自己的 ready 判定。知识服务不依赖聊天服务的 SourceAuthority 或 issuer 配置就绪；它只在
每次操作时按当前来源验证与逐次授权决定是否执行。一个服务未就绪不影响另一个服务的 liveness。

`--config` 属于命令本身，`serve` 之后的参数（`--host`/`--port`/TLS/权威名）属于子命令；顺序
写反会被 argparse 拒绝。

## 2. 绑定、权威名与 TLS

规则集中在 `src/tianshu_memory/server_runtime.py`，两个入口共用同一份实现，不存在第二套判断。

- `--host` 只接受 **IP 字面量**，默认 `127.0.0.1`。主机名不会被隐式解析；IPv6 会被规范化
  （`2001:0DB8::1` → `2001:db8::1`），带 zone 标识的地址（`fe80::1%eth0`）被拒绝。
- **回环绑定**不要求 TLS，且保持既有的宽松主机名行为（既有入口一律接受任意 `Host`）。
- **非回环绑定**必须同时给出 `--tls-certfile`、`--tls-keyfile` 和至少一个
  `--allowed-host`，三者缺一即 `invalid_configuration` 启动失败。没有任何明文 HTTP 部署可以
  从这条路径产生。
- 证书与私钥必须同时给出、必须存在且必须真的能加载（不成对、不可读、非证书/非私钥文件一律
  启动失败）；只有"两者都不给"是允许的，且只在回环上。
- 权威名比较**按名字**而不是按拼写：`--allowed-host` 里的值与请求 `Host` 头都会经过同一套规范化
  （小写、IPv6 规范化、端口必须等于本进程端口），因此 `[2001:db8:0:0:0:0:0:1]:8443` 与
  `[2001:db8::1]:8443` 是同一个权威，而 `memory.example.test:8442` 不是。
- **不信任任何转发头**。没有配置可信代理，`X-Forwarded-*`/`Forwarded` 一律不参与判断；一个
  非法 `Host` 不会因为带上了转发头而变合法。
- TLS 由 uvicorn 直接终止（`ssl.SSLContext` + `load_cert_chain`），不依赖反向代理。

浏览器来源（`Origin`、`Sec-Fetch-Site: cross-site`）在两种绑定上都拒绝，返回
`{"status": "failed", "code": "browser_origin_refused"}`；非回环上的非法权威返回
`{"status": "failed", "code": "invalid_host"}`。这两个响应只包含 `status` 与 `code`。

## 3. 两个只读探针

| 路由 | 认证 | 成功 | 失败 |
| --- | --- | --- | --- |
| `GET /health/live` | 匿名 | `200` `{"status":"alive"}` | 无（进程不在即无应答） |
| `GET /health/ready` | `Authorization: Bearer <TIANSHU_DIAGNOSTICS_TOKEN>` | `200` | 未给凭据/凭据错误 `401`；本进程未配置 token `503`；就绪判定为假 `503` |

- 两个探针的响应体都封闭为 `status`、`service`、`checks` 三个字段。拒绝与判定用 HTTP 状态码
  表达，**不往文档里加错误码、消息或运维提示**。`checks` 的键是两个服务各自的固定集合，
  值取自 `ok` / `failed` / `not_configured` / `not_verified` / `non_durable`。
  全部响应带 `Cache-Control: no-store`、`X-Content-Type-Options: nosniff`、
  `Referrer-Policy: no-referrer`。
- readiness token 只从 `diagnostics.token_env` 命名的环境变量读取，**不落盘、不进日志**；它不授予
  任何业务权限。token 在适配器构造时读取，进程运行期间不会获得新凭据。
- `not_verified` 表示"本进程没有真的测过这条依赖"（远端来源、外部依赖），按合同如实上报，
  **不阻塞** ready。
- `not_configured` 的含义按检查项区分，并逐项写在 `runtime_probes.BLOCKING_STATES` 里：
  核心前置条件（配置、合同包、数据库、guard、mode、client、日志、装配、owner）未配置即
  `not_ready`；可选能力（扩展 schema 族）未安装时只上报、不阻塞；整个进程未装配时
  `assembled` 为 `not_configured` 且必然 `not_ready`。
  一个检查键若没有任何规则提及，按失败关闭处理，不会因为漏写规则而放行。
- `--diagnostics-contract` 未给出时，合同包位置按配置里的 `contract_directory` 推导到同级的
  `contracts/diagnostics/v1`；推导不出来时报告 `not_configured`，**绝不猜测位置**，也绝不声称
  验证过一个没找到的包。

### 探针的只读性

探针自身**完全不写日志、不推进 sequence、不写任何文件**。它们按精确路径被排除在诊断中间件
之外（`/health/live`、`/health/ready`，以及产品既有的 `/health`）。数据库检查以 `mode=ro` URI
打开，不构造 `Store()`、不开 `Store.transaction`、不做 mkdir、不跑迁移、不写 guard：

- 数据库文件、`source-guard.json` 与所在目录在反复探测前后逐字节一致；
- 不存在的数据库**保持不存在**（`mode=ro` 不会创建空库）；
- 旧 schema 不会被隐式迁移；journal mode 不会被改变；
- 缺失或与库内 revision 不一致的 guard **绝不重建**。

### 就绪判定与容量故障

日志不可用时**不给出假 ready**：容量耗尽或写入失败被锁存，`log` 检查变为对应失败态，
`/health/ready` 返回 `503`，新的业务请求被拒绝（`log_unavailable` / `log_capacity`）。已经处于
在途的副作用**不会因为日志失败而重发**。

## 4. 运行日志

- 每个进程一个 JSONL sink，目录来自配置的 `log_directory`（部署挂载在 `/var/log/tianshu`），
  必须是**绝对路径**。行数、顺序、容量都按进程独立计算。
- 每行是固定的 12 字段封闭结构：
  `schema_version,timestamp,service,instance_id,sequence,event_id,level,event,outcome,correlation_id,duration_ms,error_code`；
  单行不超过 4096 字节（含换行）。没有 `message`/`extras`/`stack`/请求体/路径/ID/token。
- 事件名全量注册、不采样：`runtime.starting`、`runtime.started`、`runtime.ready`、
  `runtime.start_failed`、`runtime.stopping`、`runtime.stopped`、`request.started`、
  `request.authenticated`、`sync.execute.started`、`sync.execute.completed`、`request.completed`、
  `log.sink_failed`。未注册名字会被拒绝，不会静默丢弃。
- 分片 64 MiB 滚动；目录默认上限 1 GiB，可显式配置 32 MiB–64 GiB。写入 flush + fsync，
  单行在锁内原子落盘。
- 容量故障**失败关闭**：不删除尚未被采集的文件，不再接受新业务，只打印一次固定告警到 stderr
  （`tianshu-memory: runtime event log unavailable; readiness fails closed`）。
- `log_directory` 缺失或为 `false` 时进入**非持久模式**：此时什么都不写（没有 stdio 回退，
  避免服务进程被写满的继承管道阻塞），生产环境下非持久模式**永不 ready**。

### 关联标识

`X-Tianshu-Correlation-Id` 只接受合法的 32 位小写十六进制；非法值一律**生成新值**，绝不回显
原始输入。进程会把自己的关联标识发布在请求的 scope 上，让不 import 诊断适配器的入口也能
携带它，而不必复制任何领域规则。`correlation_id` 在日志里要么是合法值，要么是 `null`。

## 5. 容器镜像

`infra/container/Dockerfile` 是**一个镜像、两个入口**：构建阶段把仓库自己的 `uv.lock` 导出成
带哈希的依赖清单并校验安装，运行阶段只带上解释器、已安装环境与探针脚本，不含构建工具、不含
源码检出、不含测试。

- 非 root（`10001:10001`，无 home、无 shell），可写路径只有 `/var/log/tianshu` 与 `/srv/tianshu`，
  两者在镜像内创建并归属该账号；容器层用 `--read-only` 落地不可变的其余文件系统。
- 镜像内不写任何凭据、不写任何配置值：只有路径与解释器开关。
- `HEALTHCHECK` 调用 `scripts/container_healthcheck.py`（纯标准库、无 shell 工具）：
  只探测 `/health/live`，走**校验过证书**的 TLS（`ssl.create_default_context`，可用
  `TIANSHU_HEALTHCHECK_CA` 增加一个私有信任锚，但**没有任何开关能关闭校验**），连接超时 2 秒、
  读取超时 2 秒、响应上限 4 KiB；`TIANSHU_HEALTHCHECK_HOST` 同时决定 `Host` 头与连接端口。
  退出码：`0` alive、`1` 进程回了但不是 liveness 文档（唯一应触发重启的情形）、
  `2` 探针无法得出结论（不可解析的权威名、读不到的信任锚、无法验证的证书、无人应答、
  应答超限）。**liveness 不等于 readiness**：readiness 需要独立凭据，不应作为重启依据。
- `.dockerignore` 位于 `infra/container/Dockerfile.dockerignore`，必须在构建命令里显式点名
  （该文件名只有被显式指定时才生效）；它以 `**` 默认排除，只放行 `pyproject.toml`、`uv.lock`、
  `src/tianshu_memory/*.py` 与探针脚本，并按名字排除 `.git`、`.runtime`、`.venv`、测试、文档、
  数据库、备份、证书与凭据。

构建与运行（**本环境没有容器运行时，以下命令在本卡中未执行、未验证**）。
本卡不新增 compose 文件：集中编排属于下一批。基础镜像按 Python 依赖的精确补丁版本固定，
**镜像 digest 尚未在打包验收中固定，因此当前 tag 仍不代表可复现**，只代表"同一份 Dockerfile
与同一份 `uv.lock`"。镜像内不含 `.env`、数据库、凭据，也不含任何可用于真实环境的 token 或
占位配置；配置由操作者在运行期以只读挂载提供。

```powershell
docker build `
  --file infra/container/Dockerfile `
  --ignorefile infra/container/Dockerfile.dockerignore `
  --tag tianshu-memory:local .
```

```powershell
# 聊天服务：非回环绑定必须给证书与权威名
docker run --read-only --tmpfs /tmp `
  --mount type=bind,src=/etc/tianshu,dst=/etc/tianshu,readonly `
  --mount type=bind,src=/srv/tianshu,dst=/srv/tianshu `
  --mount type=volume,src=tianshu-logs,dst=/var/log/tianshu `
  --env TIANSHU_DIAGNOSTICS_TOKEN `
  -e TIANSHU_HEALTHCHECK_HOST=memory.example.test:8130 `
  -e TIANSHU_HEALTHCHECK_CA=/etc/tianshu/ca.pem `
  -p 8130:8130 `
  tianshu-memory:local `
  --host 0.0.0.0 --port 8130 `
  --tls-certfile /etc/tianshu/server.pem --tls-keyfile /etc/tianshu/server.key `
  --allowed-host memory.example.test:8130 serve
```

```powershell
# 项目知识服务：端口无默认值，身份只能是启动参数
docker run --read-only --tmpfs /tmp `
  --mount type=bind,src=/etc/tianshu,dst=/etc/tianshu,readonly `
  --mount type=bind,src=/srv/tianshu,dst=/srv/tianshu `
  --mount type=volume,src=tianshu-logs,dst=/var/log/tianshu `
  --env TIANSHU_DIAGNOSTICS_TOKEN `
  -p 18135:18135 `
  tianshu-memory:local `
  python -m tianshu_memory.knowledge_cli --config /etc/tianshu/knowledge.json `
  --host 0.0.0.0 --port 18135 --client project-web-reader serve
```

容器里**没有** `.runtime/workspace-context.json`，所以诊断合同包的位置不会被推导出来：容器部署
必须在配置文件里显式给出 `contract_directory`，并把 `contracts/diagnostics/v1` 一起挂载进去，
否则 `/health/ready` 会如实报告 `contract: not_configured` 而非假装验证过。

## 6. 环境变量

| 变量 | 谁读 | 含义 |
| --- | --- | --- |
| `TIANSHU_DIAGNOSTICS_TOKEN` | 运行时（名字由配置的 `diagnostics.token_env` 指定） | `/health/ready` 的独立凭据；不授予业务权限 |
| `TIANSHU_HEALTHCHECK_HOST` | `scripts/container_healthcheck.py` | 被公告的权威名（含端口），同时决定 `Host` 头与连接端口 |
| `TIANSHU_HEALTHCHECK_CA` | 同上 | 可选：额外的私有信任锚文件路径；**不是**关闭校验的开关 |
| `TIANSHU_LOG_DIR` | 配置作者 | 镜像内约定：日志卷的挂载点，写进配置的 `log_directory` |
| `TIANSHU_MEMORY_CONFIG` | 容器默认命令 | 镜像内约定：配置文件路径 |

绑定地址、端口、证书、权威名与身份**都只能来自启动参数**，没有任何环境变量回退。

## 7. 本卡未验证的部分

- **容器构建与运行未验证**：本环境没有 Docker/Podman 守护进程，镜像**没有构建过、没有运行过**。
  `tests/test_container_runtime.py` 只能证明（a）Dockerfile 文本里写明的性质，以及（b）探针脚本
  对真实 TLS socket 的行为；`Dockerfile` 能通过静态检查**不等于**构建通过。
- **真实远端来源、真实账号、付费调用未测试**。`remote` 检查永远是 `not_verified`。
- **NAS 操作未执行**。日志目录指向本地路径；容量上限、卷与保留策略只在本机隔离测试中验证。
- **两个服务同时对外部署、网关与网络边界未验证**：本卡只验证每个进程自己的绑定与权威名规则。
