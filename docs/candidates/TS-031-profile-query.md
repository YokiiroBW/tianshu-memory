# TS-031 跨人物与群主题查询候选（待协调发布）

2026-09-14。基线 `358e4a6`。本文不是已发布合同，不开放候选 HTTP。

## 建议的最小兼容扩展

单独发布 `profile-memory/v1` 1.0.0，新增 `POST /internal/v1/memory/profiles/select`。
保留 `text-dialogue/v1` 1.0.0 的 schema、摘要和所有本人操作；禁止把旧 select 的
`requested_scope.person_id` 解释成第三人。扩展显式依赖旧包的 query/scope/budget、
完整语义组与 shareable_projection 形状，启动分别验证两包发布摘要。

请求建议：

```json
{
  "query": {"schema_version": 1, "request_id": "query-1", "origin": {"assertion_ref": "origin-A-G"}},
  "requester_scope": {"actor_id": "actor-1", "person_id": "person-A", "audience": "group", "conversation_id": "conversation-G"},
  "target": {"kind": "person", "person_id": "person-B"},
  "query_text": "field:interest.coffee",
  "selection": ["interest", "style"],
  "known_scope_version": null,
  "budget": {"tokens": 4096, "bytes": 4096}
}
```

群主体使用 `target: {"kind":"group","conversation_id":"conversation-G"}`，不能带
person_id。此 ID 是核心已签发且来源 issuer 已核实的群会话 ID；不新增记忆自有群 ID。
一个请求一个主体，跨人物多次查询由核心共用剩余整轮预算。selection 限
`interest/style/topic`；群只支持 `topic/style`，人物只支持 `interest/style`。
任意 actor、target、purpose 都不产生权限；不提供 purpose 授权开关。

响应沿用 selected_units/dependency_groups/budget_used/verified_at/valid_until/
scope_version/omissions，加回显 requester_scope 与 target；unit 将 subject_person_id
改成 `subject`（同 target union），新增 `field_key` 与 `sharing`（group_only/public_preference），
保留 statement/conditions/negations/valid_time/uncertainty/reality/record_version。
visibility 固定 shared_projection，sources 仅 memory-owned projection_ref/version；
没有原文 source、私聊 message_key、内部好感值、隐藏条数或过滤原因。

## 三种信息与权限

1. 本人私有事实继续仅走原 v1；扩展永不把它直接作为候选。
2. `group_only`：明确审核批准的独立完整投影，仅某 actor + 精确群会话可见且适用。
   同人 G1/G2 的风格各保存一个完整语义组，不相互覆盖。群主体必须当前群，不能查询组外群。
3. `public_preference`：本人明确批准的一项普通兴趣字段，可在同 actor 已获准的群和私聊
   场景分享；不代表匿名互联网公开、跨 actor 或公开原始经历。仅人物 interest 可用；
   风格和群主题不自动全局化。普通字段名称本身不是共享同意。

来源 context 仍用现有受信 issuer：先将 verified_account 与 requester_scope 的 person、
actor、audience、conversation 精确核对，再查该受众的共享投影。群外来源、已撤销来源、
伪造 requester、actor 或会话均拒绝；会话尚未核实仍 503。目标必须稳定 person_id，
上游若用平台账号定位必须来自可信不可变账号映射；不按昵称/头像匹配、不增设查询私人账号目录。
不要求在私人 people 表探测第三人存在性：不存在和无公开可读内容均 200 空数组/no_match。

写入不新增共享 HTTP：记忆内部受控流程接收有精确目标、actor、共享范围、字段、
完整投影正文、原始来源版本及到期时间绑定的批准凭据，事务内消费一次。
本任务只提供显式 local_fixture 合成批准用于组件验证；生产确认 issuer 未接即不可用，
不能把模型标记或合成审核宣传成用户真实同意。

## 版本、最小召回与失效

字段查询先在已获准投影元数据中筛候选，语义组完整性/当前来源/投影版本再校验；
词项覆盖数 + BM25 复用 TS-030，保留条件、否定、时间和不确定性后整组预算取舍。
零预算只读请求人身份与受众版本元数据，仍检查来源后端配置与授权，不读取目标私人信息、
画像正文、来源正文或构建 FTS。

scope_version 建议为当前 actor 的公开投影 epoch 与当前群投影 epoch 的单调和（减初始偏移）；
仅已批准共享投影变更影响对应 epoch，不受目标私有记录数量/版本变化影响。
未知目标与无可读内容在同一受众具有相同版本语义。无服务响应缓存，HTTP no-store；
valid_until 不授予离线使用权。消费者发送前带 known_scope_version 零预算复核，
并把 target/actor/requester/受众/查询/版本纳入缓存键。

新投影复用现有 groups/records/lineage/projections/history/index；新增小型共享授权元数据表，
不创建第二个记忆服务或图数据库。原文更正、撤回、遗忘在同事务撤销所有依赖投影、增加对应
受众版本、写 outbox，墓碑不能靠旧批准重新发布。新增存储通过显式 schema 1→2 迁移，
保留原数据与 v1 查询；旧程序拒绝 schema 2，回滚用停写备份恢复，不做丢弃新数据的隐式降级。

## 少量验收对照

| 输入/前提 | 预期 |
| --- | --- |
| A 在获准 G 问 B 咖啡兴趣，B 批准 public_preference | 仅完整咖啡偏好投影 |
| 同请求，只有 B 私聊咖啡经历 | 200 空/no_match；与 B 不存在相同 |
| B 在 G1/G2 分别批准严谨/玩梗风格 | 各群只取各自投影；G1 条目不能作为公开偏好带入 G2 |
| A 在 G1 问 target group G1 的技术主题 | group 主体完整投影，无 person_id |
| A 在 G1 请求 group G2，或伪造 requester B | 403；零预算也拒绝 |
| 公开兴趣含否定/条件，预算少一个字节 | 整组不返回，omissions 仅 budget |
| B 更正/遗忘原来源，跨两群和公开投影已生成 | 同事务全部失效，旧版本探针409，重启后仍不可读 |
| 原 v1 本人请求及旧数据库 | 维持原行为；v1 借 target/person_id 跨人物仍拒绝 |

## 请求协调裁决

请确认独立包/路由、target union、公开兴趣可用于同 actor 的获准群及私聊、
共享版本语义；发布最小 schema + 正反实例及固定摘要后通知本任务。
批准前仅推进内部领域与迁移/测试准备，不注册新 HTTP 路由，不修改根 contracts。
