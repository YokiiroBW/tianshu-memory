# 记忆服务实现决定与正确性边界

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

来源/确认实际生产签发及撤销、Chat Audit 原文读取、真实 PostgreSQL、语义检索/嵌入评测、自动候选提炼/完整重建、跨平台账号证明、多服务 L0 与真实渠道 L1 均未完成。当前来源原文读取明确 unavailable；不冒充已归档。outbox 采用持久 at-least-once 读取/确认应用层，尚无生产投递器和已发布 memory.revised wire，不能直接把内部通知当共享合同发布。

## TS-031 共享画像领域

上文本人限制描述已发布文字合同 1.0.0；新画像领域保留这一限制。共享查询把请求人的
可信 scope 与目标 person/group 分开。群用核心已核验会话编号，存储中带 `profile_subject`
标记和独立授权投影索引，不向旧 v1 查询器提供 subject union 记录。重建索引和来源失效仍
共用现有 groups/records/lineage/history/projections，不另设服务、图库或向量实现。

本人私有记录、仅精确群可见/适用的 group_only 投影、同 actor 获准场景中可用的
public_preference 分开保存。响应含显式 category，且与本次 selection 和授权元数据一致。第三人读取不查询目标私人目录；目标不存在和只有私密内容
返回相同空选择/no_match。来源只返回新签发的投影引用，原文血缘留在服务端。完整语义、
精确字段候选缩小、词项覆盖+BM25和整组预算直接复用 TS-030；共享查询不包含关系数值。

本地 workflow 的 approve_profile/publish_profile 只在 LocalFixtureSources 下可用，
批准绑定完整主体、字段、共享范围、正文、来源 revision/epoch 和到期时间；发布前重查
当前来源与请求人。不能用已批准 group_only 的凭据发布 public_preference。
本人在群里主动披露的兴趣，也可在单独明确批准公共范围后生成公开投影；仅来源在群内
不构成全局授权。group/style 不能按 public_preference 发布，群主题不得取个人私聊来源。
人物场景观察可标 inferred；观察、条件和不确定性按原字段保存。

生产可使用已确认的类别/范围共享策略和获准群整理策略，不要求逐项弹窗；本任务没有
实现生产同意/策略 issuer，也没有把合成批准当真实用户同意。群批准在这里是夹具整理，
不是让任意真实成员代表群授权。真实来源适配必须在同一权威事务里传播修订和撤销。

版本域为 profile-memory/v1，与 text-dialogue/v1 的 scope_version 独立。
共享版本是 actor 公开 epoch 加当前群 epoch（初始偏移去重）；私聊仅用公开 epoch。
私人记录变更不影响共享版本，除非它使已批准共享投影失效；其他群独有变化不影响当前群。
共享组仅在 active→invalidated 首次退出时推进共享 epoch；已撤回组的后续私人来源修订
继续维护历史/墓碑，但不发出新的共享版本信号。旧 v1 的范围和历史语义保持独立。
发布、修订、遗忘和来源撤销事务内更新对应 epoch。无响应缓存；复核必须用本版本域端点，
相等整数不代表可替换旧 v1 探针。发送前校验与渠道发送之间仍非分布式原子。

数据库 schema 1→2 由显式 migrate-profiles 执行，事务前留完整 SQLite 备份；新增两张小表，
不改写原有记录。新版程序可继续开 schema 1 提供 v1；共享查询需要 schema 2。
备份读连接和目标连接在成功及异常路径均显式关闭，不依赖 SQLite 事务上下文或垃圾回收释放文件。
旧版程序拒绝 schema 2。回滚需要停写并恢复备份，迁移后新增数据应另行保留，不提供会
丢弃它们的原地降级。现有私有数据不会在迁移时自动生成共享批准或投影。

组件测试通过不等于 BOT 消费新画像；陪伴核心客户端、生产策略/来源、PostgreSQL、
embedding、真实 L0/L1 均留待各自任务联合验收。
