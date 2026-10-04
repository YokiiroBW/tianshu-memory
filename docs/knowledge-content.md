# Knowledge 生活内容原件

本服务是内容唯一 owner。项目 import 与生活 acquisition 共用 `knowledge_originals.store_version`、`knowledge_documents/versions/blocks/index`，保留原 bytes、sha256、版本和原始地址。生活文档的 `project_id=NULL`，范围由真实角色或精确人物受众拥有，不创建假工程项目；Companion 只保存引用、阅读范围和进度。

正式合同为协调者发布的 `knowledge-content/v1`，单向依赖 `life-runtime/v2` 和 `text-dialogue/v1`，唯一 pin 在 `contracts.py`。部署装配会核验所有包、依赖 manifest 与文件 hash，不从网络加载 schema。现有 Memory HTTP 和独立 Knowledge HTTP 入口复用同一内容模块；生产独立 Knowledge 使用自己的原件数据库，无新内容服务或 Companion 副本库。

## 准备与实际配置

数据库先完成既有 schema 3 与 Knowledge 迁移，停止写者后执行：

```powershell
uv run python -m tianshu_memory.knowledge_cli --config <既有Memory私有配置> migrate-content --backup <新的完整数据库备份文件>
```

迁移原子重建既有三张原件表的 FK 图，使 project 可空；旧原件、blocks、目录索引及项目权限保持。四张新增内容表与三个原件表的写入均受现有 source-guard 追踪；不关闭 FK、不重置 checkpoint。返回 backup 与 guard_backup（backup文件名加 `.source-guard.json`），在同一已核验的 Store 写事务中独占生成精确匹配的升级前快照。C1 账本和来源账本不变。

恢复演练将 backup 与 guard_backup 一起复制到一个新的隔离数据库路径及其 guard 路径，然后用 Store 验证并检查非空原件/blocks/FK与旧 revision。实际停写恢复需要协调者审查升级后写入/来源 suppression 与高水位的处置；不能把旧数据库直接盖回当前 guard，也不能用旧 guard 覆盖当前高水位来绕过恢复审查。本实现不自动执行生产恢复。

在现有 `callers.companion` 注册 `content_acquire/content_read/content_original`，按需要注册 `content_uploads/content_upload_status`。角色自主分支另需 `runtime_content:true`，沿原 `allowed_actors` 或 `allow_runtime_roles:true` + 已配置 `role_grants_database_path`；每次检查真实 Platform 注册的 RoleGrants，disabled 总拒。C3 每个实际动作检查自身 runtime/life epoch，principal 的 operation_ref 指向真实持久 activity/reading。没有伪人、伪账号或伪入站 assertion。

Platform 已登录用户上传/管理沿其既有 issuer/token、真实 origin/account binding 与精确 scope。`content_access` 只给已注册管理调用方；actor 管理还需 `content_actor_access`，普通模型/角色 reader 不获得 grant 工具。QQ 实际附件发送只物化角色有权读的原件 bytes，Platform 用原实际收件人与主动许可查权后发送；发送附件不会自动授予网页原件读取权。网页打开仍按 Knowledge 精确 scope 的对象 version/sha 授权。

独立 Knowledge 仍以 `knowledge_cli ... serve --client <既有项目客户端> --port <显式端口>` 启动，保留原 `/local/v1/project-knowledge/action` 及其固定 client。配置任一 `content_*` caller 后挂载下列正式内容路由；配置 `callers.platform.role_admin:true` 后同进程挂载 `/internal/v1/role-runtime/authorize` 的 apply/status。Platform 使用现有 durable RoleRuntime stages 分别同步 Memory 与 Knowledge 的 `role_grants_database_path`；角色授权文件为 Knowledge 自己的绝对路径，不读 Memory DB。未配置内容 caller 的旧项目服务行为保持。

每个内容 caller 在 `callers.<名称>` 保留独立 `token` 和所需 `operations`。用户 caller 需要既有 `issuer:"platform"`、实际 HTTPS `issuer_url`、`issuer_token`，自签部署使用绝对 `issuer_ca_file`；角色自主 caller 需要上述 runtime_content/allow_runtime_roles。Web 管理 caller 配置 uploads/upload_status/acquire/read/original/access，角色 runtime reader 配置 acquire/read/original；Platform role_admin caller 不自动获得内容权限。生产不能使用 local_fixture 或 origins 配置模拟真实账号。

独立 Knowledge 的人物身份不依赖其本地 people/accounts 表。每次实际用户操作由 Authenticator 实时向 Platform resolve origin：Platform 复核当前 entry、owner、撤销、摘要、期限与 route，然后投影由 Memory 真实回执确认的当前身份/渠道精确 scope。Knowledge 只接受该 exact scope，拒绝请求自行替换 actor/person/audience/conversation；不会复制身份，也不声称每次 HTTP 又向 Memory 查询。owner 显式 grant 只保存同 actor 的完整 reader_scope 和原件 version/hash，不创建人物或 origin。即使保存了不存在的目标 scope，没有 issuer 的当前有效 exact origin 仍不能读取；撤销 grant 或 origin、停用角色后即拒绝。

`knowledge_content.trusted_urls` 是可选的部署登记精确内部来源，不需要为每个公共 URL 登记。默认用户/角色提交的公共 HTTP/HTTPS 地址逐跳 DNS 公网检查、连接地址 pin，HTTPS 验证证书；非公网仅允许配置中完全匹配的 URL，重定向也逐跳核验，禁用代理、压缩响应与凭据 URL。既有项目 import 的默认 exact HTTPS allowlist 保持。

## 正式入口

POST `/internal/v1/knowledge/content/{acquire,read,original,uploads,upload-status,access}`。

principal oneOf：user 为 `{kind:"user",query:<common.query>,scope:<common.scope>}`，actor 为 `{kind:"actor",request_id,actor_id,operation_ref}`。actor 对应 Life scope=null，仍是私有角色空间。请求 ID 是持久幂等/恢复键，重新请求换 query ID；同键不同语义为 409。所有读取重验当前凭据、角色、owner/reader、原件 version/hash 与撤回状态，解码后再核权才返回实际内容。

`uploads` 注册真实 filename/media_type/size/sha256 后取得 upload_id；PUT `/internal/v1/knowledge/content/uploads/{upload_id}` 发送真实 `application/octet-stream` bytes。user 带 `X-Tianshu-Assertion-Ref`，actor 带 `X-Tianshu-Actor-Id` 与 `X-Tianshu-Operation-Ref`，可带 `X-Tianshu-Request-Id`。先验权与登记 size，再读 body；严格检查完整 size/hash。中断保持 pending，upload-status 可查询，重新发送整 body；相同原件重传可恢复 complete/imported 回执。有效期一小时；纳管成功后暂存 bytes 清空，唯一永久原件在 knowledge_versions。

acquire 支持真实 URL 或 upload_id，返回 owner=memory 的稳定 content_ref。相同来源 hash 更新产生下一版本；旧 ref 不再可读，旧授权绑定 version/hash 因而失效。withdraw 保留历史 bytes/回执但停止原件和范围读取。grant/revoke 只改变该对象精确 reader_scope 的权限，不改变 person/关系或角色身份。

read 返回实际 text 或 representations。original 返回经过同样权限检查的真实 bytes，`Cache-Control:no-store`，无永 public URL；`X-Content-SHA256` 为实际返回 bytes hash，`X-Source-SHA256` 为完整原件 hash，`X-Content-Version` 与 `X-Content-Coverage` 绑定版本/字节范围。original 可选 bytes 范围，实际 MIME 在 Content-Type。

## 真实格式与边界

- UTF8 文本/Markdown、HTML 可见正文和 DOCX 实际段落/表格：characters 0 基范围。保留原件全部 bytes，响应最多100000 UTF8 bytes，截断返回 range_truncated。
- PNG/JPEG/WebP/GIF：实际图片 bytes，完整 bytes 范围才可视觉解码；预算不足返回真实缩小 JPEG，显式 image_resized，保留 source hash 与 representation hash。
- PDF：真实 pages 0 基范围，每次最多16页；文本层实际提取，未取得页图片显式 page_images_not_extracted，扫描页 no_text_layer，不宣称完整页面理解。
- MP4/WebM/QuickTime：PyAV 实际 seek/decode，请求 seconds 跨度最多30秒、最多8帧、解码最多4096帧/5秒。返回每帧真实 timestamp、实际首末帧范围；frames_sampled/audio_not_transcribed、complete=false，不能标全部视频理解。
- WAV/MP3/Ogg/MP4 音频：真实解码为16kHz mono PCM/WAV 片段，返回实际起止 seconds；audio_not_transcribed，无虚构转写。

单原件最多32MiB；图片/视频画面最多2000万像素；DOCX 解压总量最多32MiB；总文本提取最多4MiB。read 总解码 bytes 预算最多1.5MiB（再 base64 编码），媒体 bounds 不适用则400/416，过大413，不支持/坏编码415，缺失/撤回404，版本/幂等/授权变动409，依赖503。超时408，写入执行已开始而结果未确认时返回 unknown，调用方先查回执/状态而非声称取消。

source.acquired_at 是取得回执时间；URL source_time 只使用源实际有效 Last-Modified（未提供则null），上传无源时间则null，不冒称用户发送或内容发表时间。available_until 是短期授权可用期限，每次实际读仍重验。

Pillow、PyAV、pypdf、python-docx 是实际解析依赖，已锁定 binary wheels。既有 Docker Python3.12 Bookworm（glibc2.36）可消费锁中的 PyAV manylinux_2_28 x86_64 wheel；无需借用 Chat Audit 的内部 ffmpeg 或安装新 NAS 服务。当前验证为 Windows 的实际 codec/HTTP/重启和隔离安装；Linux 容器与跨产品/NAS验收由协调者完成，不据此声称生产可用。
