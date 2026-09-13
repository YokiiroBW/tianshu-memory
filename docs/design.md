# TS-030 实现决定与正确性边界

## 技术选择

FastAPI 提供小型 JSON HTTP 边界；jsonschema 从主发布目录装载 Draft 2020-12 schema 并禁用隐式远程解析；HTTP 来源解析用 httpx 的显式固定 HTTPS URL、超时、禁止重定向与环境代理。SQLite/WAL/FTS5 使用标准库，单操作 `BEGIN IMMEDIATE` 保持版本、写账本、语义组与 outbox 同一原子提交。此选择适合隔离首轮验收，生产 PostgreSQL、迁移工具和多实例吞吐尚未实现。旧数据库 schema 不自动导入；不识别的本产品 schema 版本拒绝启动。

只读审查了 `references/legacy-memory/src/tianshu_core/adapters/sqlite/memory_repository.py` 的 WAL、FTS5、中文双字切分思路；没有迁移其权限模型、数据库或通用知识框架。新代码针对 1.0.0 契约独立组织，不跨项目导入内部模块。

## 数据与信任

人物按 namespace/immutable_account_id 唯一登记；昵称是展示字段，不参与归并。同平台群私使用同 person_id，不自动关联跨平台账号。账号关联端点保留合同，因真实双账号证明流程未实现而返回 503。

首次 register 接受来源中尚未生成的 person/conversation null。select 的本人 person null 可由当前账号绑定补足，但 conversation 必须由来源解析提供精确 ID；memory 不拥有 companion 的渠道会话映射，不把来源中的 null 当任意会话授权。尚未解析出会话时返回 dependency_unavailable/503，不执行查询；非空而不匹配则 forbidden/403。协调的现有合同闭环是接入方仅在收到可信核心 ingest_response 后记录 verified channel→conversation，issuer 后续 resolve 返回精确 ID，无新增共享 wire。测试覆盖 null→可信 issuer 补足→成功与明确不匹配拒绝；跨产品真实闭环仍待 L0。

有 origin 的注册、查询、更正每次先验证独立服务令牌，再按服务固定 issuer 解析来源，与当前绑定、允许角色与精确 scope 取交集。过期或撤销来源不能凭幂等回放绕过授权。幂等键按 service/operation/key 持久化，payload 与 expected_version 不变；request_id、deadline 和 origin 可以更新。隐藏记录更正统一 404。

turn_committed 使用 companion 服务身份及服务器配置的 exact `event_scopes`。事件不新增必填 HTTP 来源引用头，不依赖旧会话来源仍有效。owner、scope、turn/input_revision、aggregate_version 与当前来源均核验。初见 committed 可是 aggregate_version>1（前序版本属于 companion 内部轮次）；已观察的版本缺口返回 503，待 owner snapshot 接口接通，不造中间状态。event_id 冲突返回 409，HTTP 边界另事务记录冲突摘要；原文不写告警。

`source_authority` 是服务内部来源核验端口，默认未配置。唯一附带实现 `LocalFixtureSources` 明确读取合成来源 ledger，不是生产签发。HTTP 不开放直接创建来源、确认、更改账本的任意写入口。真实来源修订、撤销传播与一致性须由外部适配完成并单独联合验收；没有猜测 Chat Audit 写入/读取 URL。

## 写入与失效

提交事件原子写 inbox、turn/input_revision 去重、候选 job 与 outbox；响应的 confirmed_memory_written 永远 false。最多 256 个 pending 工作，满时 429、未接收。模型生成完成或 confirmed_user_correction=true 不符合此入口。候选持久化后可以重启继续，由内存服务拥有的应用层接收完整、已审核结构化语义组，或 [] 明确跳过；没有每句话自动调用 LLM。现有 CLI 只用于合成夹具审核，不是生产确认入口。

写入前复核当前来源 revision/epoch、撤销状态、scope_version；job ledger 和来源版本/scope ledger 防止重复记忆及关系累加。来源重复涉及部分新内容时保守整次拒绝二次记账，不自动拆分；后续提炼策略需处理这种取舍。关系可作为选定语义组读取，数值贡献保存为去重条目，失效组不再参与累计。event 的 delivery_state 保留在工作记录，unknown 不自动生成角色已承诺事实；提炼由审核语义输入决定，事件本身不产生事实。

来源撤销和整组修订同时把受影响 pending job 标记 stale_source/scope_changed，释放候选队列容量并保留审计记录，不让不可执行的旧工作永久占槽。

更正需要一次性、精确绑定 payload/账号/scope/到期时间的 confirmation。当前选择原子失效方案：保留旧语义历史，新版本清空旧条件/否定/时间，整组退出召回。原文引用关联的所有组及共享投影一起失效，source epoch 更新并撤销旧输入，scope_version 增加；不等索引重建或通知到达。遗忘使用墓碑，历史是服务端审计记录，尚无物理擦除/备份清除功能。自动语义重建尚未实现，新事实须来自新的有效来源和完整审核语义；不能让旧 job 把撤回内容写回。

1.0.0 没有恢复操作，`correct` 不能作用于 tombstoned 记录，即使 expected_version 正确并提供新的有效来源/确认，也返回 `invalid_input/400`。拒绝不消费确认、不更新版本或历史；原幂等 forget 重放仍返回原 tombstoned 成功回执。

## 检索、范围和预算

scope 是 actor/person/audience/conversation 四元组，首切片仅本人。先在 SQL 中筛选 scope/category、组状态，再检查成员完整性、来源权威、投影版本；只有这些记录被复制到临时 FTS5 索引查询。全局旧索引只提供当前 record_version 的条目，查询不会先搜索私人全文再过滤。无权组数量或 ID 不进响应，零匹配统一 no_match。服务不保存响应缓存，并返回 Cache-Control: no-store。

`field:<精确键>`、`item:<精确键>` 先在带 scope 的 SQL 条件中缩小组候选，使用相应元数据索引，只读命中组正文，完全跳过 FTS。其余查询使用真实 FTS5：NFKC/casefold 的非中文词项与中文双字，不将中文单字作为查询依据；过滤时间、问候和通用问句词项，使“晚上/白天”等上下文不能独自命中兴趣。最长 64 个词项。通过权限/当前版本/完整性核验后，每个完整组作为一个临时 FTS 文档；先按不同查询词项覆盖数降序，再按实际 BM25 得分，最终相同分数才按组 ID 排序，之后执行整组预算取舍。私人或失效组不参与评分语料。

这是保守词项门槛与词法相关性排序，不是中文分词模型或语义理解。单字主题、只给时间的含混询问会漏召回；双字巧合、同义间接表达和多个相关主题仍需人工评测，预算充足时可能返回多个部分主题匹配。没有接入向量、嵌入、语义重排或自动同义合并。既有本地数据库应运行 `rebuild-index` 更新切分；旧索引中的单字不会被新查询当作命中依据，权威版本过滤仍生效。

普通兴趣发布到群范围需显式审核的独立 group draft。服务签发全新 projection_ref/version；群响应仅包含这个 memory-owned 引用，私聊 message_key/receipt/archive_state/locator 保存在 lineage 服务端。原文 pending 仍保持 pending，不因共享兴趣而变 archived。语义审核负责选择可公开表述，当前没有自动脱敏模型；任意模型没有此写入口。

一个命中必须带齐组成员和所有语义限定，任何缺失/失效/不可读则整组不取，预算不足则整组省略。服务有 32 单元硬上限。`budget_used.bytes` 是 canonical UTF-8 JSON 的 selected_units/dependency_groups 装配块字节数（空选择为 0）；`tokens` 使用同数字作保守估计和预留，不是实际模型用量，不宣称适用于任意未知 tokenizer 的精确上界。字段与数组开销一并计入；HTTP 响应的关联元数据不算注入块。目标 tokenizer 未提供，严格执行 byte/条目上限，token 字段同时约束估计；响应头和 /health 标明算法。

合同 select 没有 turn_id/历史装配字段，跨次累计预算由 companion 扣除已装配块并去重后传剩余额度；memory 不假装能凭 query_text 识别同一轮。任一预算维度为零时，仍核验当前身份、精确目标、权威 scope_version 与来源后端是否配置，随后直接返回空选择/0预算；只读账号/范围元数据，不查询组/记录/来源正文、不建临时 FTS、不复制索引全文。范围版本在来源撤销/修订事务内更新。SQLite authorizer 测试直接禁止这些正文读取与虚表创建，保留 null 会话503、错配403、旧scope409 断言。valid_until 不是离线授权；失效通知丢失时仍须最新查询，跨服务检查到发送之间不是分布式原子授权。

## 尚未通过的范围

来源/确认实际生产签发及撤销、Chat Audit 原文读取、真实 PostgreSQL、语义检索/嵌入评测、自动候选提炼/完整重建、跨平台账号证明、跨人物/全群主题、多服务 L0 与真实渠道 L1 均未完成。当前来源原文读取明确 unavailable；不冒充已归档。outbox 采用持久 at-least-once 读取/确认应用层，尚无生产投递器和已发布 memory.revised wire，不能直接把内部通知当共享合同发布。
