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
