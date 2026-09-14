# project-knowledge/v1 候选（未发布）

TS-080 同产品 CLI 和 MCP 工具形状，供协调者审查。根 contracts 才是跨产品正式发布入口；本目录不是已冻结合同，不改既有 text-dialogue/profile-memory/source-sync。

参考 `schema.json` 和 `examples.json`。CLI envelope 的 operation 与 arguments 对应工具：query/recover/check/status/import/write_state/delete；MCP 工具由官方 SDK `tools/list` 输出具体输入 JSON Schema，函数名称见项目资料文档。认证来自 stdio 进程配置 client + 独立凭据环境变量，project_id 仅选择已授权范围，不能扩大授权。

双方待验：Hermes 真客户端工具发现、显式写回展示、缓存 check 行为；跨产品登记主机/根目录生命周期。当前不提供跨产品 HTTP、共享数据库、全局晋升或自动模型整理。
