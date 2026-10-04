# C5 独立 Knowledge 入口修复

基于 Memory `fe767774a580e8f3f17c6ceaf229770d5fc6b48d`。生产 `knowledge_cli serve` 原先只有旧项目 action 路由；现薄挂载既有 KnowledgeContent 与 RoleGrants apply/status，Memory 与 Knowledge 共用路由注册，不复制内容业务或 identity owner。旧项目 `/local/v1/project-knowledge/action`、固定 client、Host/Origin 和有界准入保持。

独立 Knowledge 使用原件 DB 与自己的 RoleGrants DB；已登录用户每次通过 Platform 当前 origin exact scope 授权，不再要求 Knowledge 本地 people/accounts 有 Memory 人物副本。owner grant 同 actor、完整 reader scope、version/hash，不能产生身份；伪造目标读取 origin、已撤销 scope/grant、disabled 角色均拒绝。Platform 的当前映射来自 Memory 实际确认回执；这里没有新人物查询端口，也没有声称每次向 Memory 额外 RPC。

配置/初始化：保留现有 knowledge.clients 和固定启动 client；设置 contract_directory、独立 role_grants_database_path（绝对路径）；callers.platform 配 token/role_admin:true，实际 Web user caller 配 content_uploads/content_upload_status/content_acquire/content_read/content_original/content_access 与 Platform HTTPS issuer；角色 reader 配 runtime_content:true、allow_runtime_roles:true 和实际 content 操作。Platform 沿现 durable RoleRuntime stages 同步独立 Knowledge peer。数据库保持既有 schema 3/Knowledge 迁移，停写运行 `uv run python -m tianshu_memory.knowledge_cli --config <Knowledge私有配置> migrate-content --backup <新SQLite备份>`，备份与 `.source-guard.json` 成对恢复要求沿 [C5 原交接](C5.md)。启动：`uv run python -m tianshu_memory.knowledge_cli --config <Knowledge私有配置> serve --client <原项目客户端> --port <显式端口>`，TLS/host 沿原部署参数。

验证：独立 Knowledge CLI 子进程、独立空身份 DB、真实 socket bytes 的首次上传、原件 sha、显式共享/撤销、伪造读取范围、origin 撤销、角色动态启用、重启读取、停用拒绝；旧项目 action 与角色授权专项合计 52 通过。新增测试 issuer 使用明确隔离 fixture；真实 Platform HTTPS + Companion + 独立 Knowledge 的联合仍由协调者/C4 验证，未冒称生产完成。未重跑 codec 或全产品。ruff check 与所改 Python format/diff 检查通过；未推送、部署或写真实数据。
