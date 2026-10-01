# 2026.10.01-rc.1 正式关系合同绑定

2026-10-01，隔离发布分支，固定交付基线 ba03202c44648a3820f7552629edd9eb5797d424。领域、权限、Store 和迁移代码零改动。测试读取协调仓库 contracts/role-relationship/v1，schema LF SHA256 e96397bac2b6ad8ff9d23c023d7d3c5ba0701734b27053a05b9d0f65a7ff8ee6；根发布提交 37b086f66ca8521ec065578b9126111d97d0f112。

本轮实际使用既有开发解释器、明确 TIANSHU_CONTRACT_DIRECTORY 与 TIANSHU_TEST_CERT_PYTHON，执行 tests/test_relationships.py、test_relationship_edges.py、test_relationship_source_sync.py、test_relationship_history.py：77 passed / 2 warnings，9.91s。真实回环 TLS 使用合成账户/来源，未调用生产 QQ、模型或 NAS。初次路径设置和缺少 .runtime 父目录导致的导入/setup 错误不算通过，修正后完整专项执行通过。

原交付全量 1080 passed / 1 failed / 6 skipped 是此前记录，本轮没有重跑完整套件。唯一既有失败为 test_confirmation_rejects_mismatched_authority[account-403] 的 400/403 差异，已在干净基线复现；六个缺 MCP 跳过保留。没有通过删例、deselect 或自动安装掩盖问题。

本提交只绑定正式合同和更新说明；生产数据库/来源检查点、Platform sidecar、Companion、Knowledge 的一致备份、显式迁移和恢复验收仍是部署门禁。未推送、未部署，不表示真实使用验收。
