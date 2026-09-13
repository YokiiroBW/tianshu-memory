# 候选语义与兼容性

待协调复制发布，不是冻结合同。新增 POST `/internal/v1/memory/profiles/select`，
请求/响应定义 `profiles#select_request/select_response`。只读；无共享 HTTP write。
依赖 `text-dialogue/v1` 1.0.0，LF manifest SHA256
`81e6cc4ddef7c6f82e055d4cb04b090db036dd5c52763473ce697aa02db478a1`。

- 请求人由原 issuer 来源与 verified_account 绑定，精确核对 requester_scope 的全部字段。
  target 是数据目标，无授权含义；人物只用稳定 ID，群只用已核验的核心会话 ID。
  群 target 只能当前获准群。昵称/头像/任意 purpose 不可用作身份或权限。
- public_preference 限人物 interest，已批准同 actor 的获准群及私聊共享。
  group_only 限精确群内的人物兴趣/风格或群主题/风格；适用性与可见性同时核验。
  category 在授权投影元数据保存，不允许把 group/style 标成全局 interest 绕过发布规则。
- 共享批准来自受信内部端口。允许已登记的群观察/整理策略，以及本人已确认的
  字段/类别/范围共享设置，不强求每条人工弹窗。模型不能自签批准；普通字段不自带同意。
  本组件只实现 local_fixture 精确合成批准，生产授权策略/确认 issuer 未接即不可用。
  群主题只来自该群可见来源/已登记说明，不能由个人私聊推成全群事实；观察可标 inferred。
- v1 所有端点保持原本人限制。新存储带专用主体标记与筛选，不能混入旧查询。
  unit.subject 必须等于 response.target；所有单位/依赖组 ID 及完整成员相符，当前来源和
  projection_ref/version 同时有效。条件、否定、时间、不确定性不裁短。
- 固定 `version_domain=profile-memory/v1`。scope_version 是公开 epoch 与当前群 epoch 的
  单调和（初始均1、群场景减1），私聊只取公开 epoch。它与 text-dialogue 的版本域完全不同，
  不得因整数相等复用旧探针/缓存。目标不存在或仅有私密内容：同受众同版本，200空/no_match；
  不查询目标私人目录。私有变更仅在影响已发布投影时才改变相应共享 epoch。
- 无隐藏数量、私密存在性、私人过滤原因、原文血缘、好感数值。输出来源仅投影引用。
  no_match 不表示人物不存在。无可读内容和未知目标可只回显输入target，不证实其存在。
- 字段查询先缩小已获准候选；其余复用词项覆盖+BM25，整组后预算。零预算仍核验来源、
  请求人、目标边界和版本，但不读正文/来源正文或构建FTS。bytes 为完整装配块UTF-8大小，
  tokens 为同数值保守估计，不冒充模型实耗。多目标/补查共用核心整轮预算。
- 更正、遗忘、来源撤销同事务使所有依赖投影及相应epoch失效；无服务响应缓存，no-store。
  消费者缓存键含本版本域、actor、请求人、target、受众/会话、query及版本。
  发送前可用本接口零预算+known_scope_version复核；不声称与外部发送分布式原子。

关系验收（schema 无法比较两个任意 ID，由服务校验）：错请求人/actor/受众/会话403；
群target与当前群不同403；null来源会话503；旧known_scope_version409；
响应target/requester/request_id须回显请求；unit.subject必须等于target；
依赖不全或来源/投影失效则整组不取；公开兴趣限人物interest；预算不足整组省略。
错误复用 v1 common#error，不增加新错误领域。

运行：`uv run python docs/candidates/profile-memory/v1/validate.py --dependency C:/YOKI/Codex/tianshu-peiban-bot/contracts/text-dialogue/v1`。
12个结构正反例；产品关系、隔离存储和真实本地HTTP验证由 TS-031 测试负责。
新增接口自身不代表陪伴核心或真实渠道已消费画像；客户端接入单列后续任务。
