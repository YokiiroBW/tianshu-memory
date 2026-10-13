# 可选语义检索

本实现扩展现有 `MemoryService.select` 和 `MemoryContext.query`，没有第二份记忆账本。

- `keyword`：原全文检索排序。
- `vector`：完整记忆组向量的余弦相似度。
- `hybrid`：全文与向量各取候选，用 RRF（常数 60）合并。
- `rerank` 可配合任一模式。候选数 1–100，默认 40；最终仍按调用方 limit 和完整组字节预算返回。
- 默认关键词、关闭重排序。精确 `field:` / `item:` 和零预算探测不调用检索模型。

网页入口为平台 `#/settings/10`，嵌入和重排序各自保存 API 基础地址、模型名及密钥，支持独立连接测试。平台拥有配置与加密凭据，Memory 只向网关传递已授权的记忆组，网关向平台读取精确配置后调用模型。原文不会绕经平台配置服务。

## 部署接线

在 Memory 原配置中增加可选项（下列均为占位符，不是生产默认值）：

```json
{
  "retrieval": {
    "url": "https://GATEWAY_HOST:PORT/internal/v1/memory-retrieval/execute",
    "token": "DEDICATED_MEMORY_GATEWAY_CREDENTIAL",
    "ca_file": "/absolute/path/to/internal-ca.pem"
  }
}
```

公网信任链可以省略 ca_file；从不关闭 TLS 校验。网关须设置 `memory_retrieval_credential_ref`，它引用此专用凭据，并给平台登记的 Gateway 服务身份增加 `memory.retrieval` action。平台复用既有 `web_external` 加密目录、`provider_self_service` 网关连接；无新权威数据库迁移。未设置 retrieval 时完全保持旧模式。

所有模式都在本来的 actor/person/audience 范围、来源有效性和 suppression 检查后提取候选。送给模型的只有 statement、conditions、negations、valid_time、uncertainty、reality，不传账号、来源定位符或 receipt。模型 I/O 不占 SQLite 权威写事务；返回前重新检查来源、范围版本、组成员、记录版本和投影。

## 缓存与运行边界

向量是进程内有界派生缓存，最多 4096 组，键包含嵌入空间指纹、组 ID 与完整文本摘要；不是持久向量数据库。更换模型/地址/连接版本会重新嵌入，重启后按需重建；记忆范围版本变化清空派生缓存，竞争中的旧结果不会重新填回已清空缓存。保留当前权限的数据库查询永远先于缓存读取。

冷缓存按每批 4 组调用嵌入，完成的批次可用于后续查询。单次排序预算 10 秒，既有 HTTP 执行上限仍生效；大库首次查询可能暂不可用，需要后续查询继续补齐缓存。context 查询每个已授权范围最多检查最新 4096 组，截断明确标 coverage 不完整。此版本没有离线全库预建索引或 ANN，不能宣称大规模检索性能已验证。

超时、配置变化、错误向量维度、非有限分数或模型不可用均明确失败，不把关键词结果冒充向量成功；可在网页切回关键词。模型只排序，事实更正、时间条件、遗忘与权限仍由原账本决定。

自动记忆提炼开关独立于检索模式，本改动不修改它。

## 2026-10-14 召回质量第一轮

关键词候选在排序前检查查询主题、明确对象与引号内标题，操作用语在引号外清理；多问句分别匹配，低相关匹配可以返回空结果。此规则由 `recall_relevance` 统一供普通召回、memory-context 和画像检索使用，仍保留完整记忆组和原授权/来源检查。它是中文优先的词法筛选，不是通用语义理解；向量与重排配置保持原状，不能据此推断任意语义检索的准确率。

自动抽取协议 3 在原 fact 中增加可空的 `entity`：`id`（新对象为 null）、`kind`（稳定类型）、`mention`（必须出现在本次引文中）。Memory 在现有范围内从仍有效的事实解析或生成对象 ID，并以“属性 + 对象”执行原去重、更正和历史保留。两个物品允许具有相同属性和值；相同对象也能拥有多个不同属性。外来、已撤回、无引文或歧义对象引用不能覆盖现有对象。旧版事实继续可读，不自动猜测并回填其对象身份。来源、权限和持久化事务仍只有原账本一条路径，无新增表或迁移。

对象匹配目前支持明确名称/标签、同编号的有限简称；含糊代词、未知别名、对象改名与同名多对象仍需更充分的来源，不保证自动识别。新版 Memory 应与支持抽取协议 3 的 Companion 成对交付；旧 Companion 客户端会拒绝协议 3。

`tests/fixtures/recall_quality.json` 保存合成查询、明确预期记录和 regression/fresh 标签。`python tests/evaluate_recall.py --output .runtime/recall-quality.json` 输出逐项漏召/误召、记录级 precision/recall 和未知问题弃答结果。设置与 pytest 相同的合同环境；通过 PYTHONPATH 指向固定旧版 src 可做同数据对照。此测试使用合成 LocalWorkflow 入库，不调用外部模型，不代表真实模型答案质量或生产性能。

对象专项 `tests/test_fact_entities.py` 使用原生候选提交、受来源保护的库和合成 HTTPS 所有者，验证分离、去重、局部更正、错误引用和遗忘；真实 DeepSeek 结果须独立记录，不能用这些固定模型提议代替。
