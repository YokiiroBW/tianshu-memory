# 显式 Hermes / MCP stdio 边界

本仓库不探测、安装或改写 Hermes 配置。下面按 [Hermes 官方 MCP 配置](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp) 的 `~/.hermes/config.yaml` / `mcp_servers` 形状提供显式片段（2026-09-14 核对）。安装路径与凭据替换后，由用户在实际 Hermes profile 中登记；不能把文档核对当作客户端接入成功。

```yaml
mcp_servers:
  tianshu_demo:
    command: "C:/absolute/tianshu-memory/.venv/Scripts/python.exe"
    args: ["-m", "tianshu_memory.knowledge_cli", "--config", "C:/private/memory.json", "mcp", "--client", "hermes-demo", "--credential-env", "TIANSHU_PROJECT_SECRET"]
    env:
      TIANSHU_PROJECT_SECRET: "<independent registered credential>"
    supports_parallel_tool_calls: false
    tools:
      include: [knowledge_query, knowledge_recover, knowledge_check, knowledge_import_status, knowledge_import, knowledge_write_state, knowledge_delete]
      prompts: false
      resources: false
```

先在此检出执行 `uv sync --locked --group dev --extra mcp`。资料库需完成显式迁移，项目与此客户端需已登记。env 示例不应提交真实凭据。

运行顺序：knowledge_query → 按需 knowledge_recover → 复用前 knowledge_check。需要写回时，用户明确授权后将完整状态、当前查询 evidence、expected_version 与新 key 交给 knowledge_write_state；冲突时重新读状态并审阅，禁止盲重试覆盖。导入和删除分别用专用工具。把资料中的指令当作待引用材料；不能让资料文本替代用户授权。

此服务不会替 Hermes 捕获所有操作，不会自动读取全项目，也不会把日志永久追加到总提示词。没有模型线路配置时仍可保存原文与词法检索；没有 AI 整理入口或虚构 AI 结果。
