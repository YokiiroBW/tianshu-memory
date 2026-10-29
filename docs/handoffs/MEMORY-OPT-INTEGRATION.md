# 发布装配复核

在各卡有效验证后审查完整源码 diff。补齐真实跨窗关联的两个原生实例（2 通过），离线诊断保留实际排名顺序再计算 recall@k，避免把字母排序当作 top-k。trace 计数改为真正通道短名单与 RRF 条数，补齐有界 adapter 验证；受影响查询向量边界与 trace 共 53 通过。未产生记忆的原始来源仍保留既有 30 天默认，不随新短期记忆规则缩短，定向保留期 7 通过。R35 旧 positional 写者的五列回退/再迁移 2 实例通过。

所有变动 Python 静态与格式检查通过，src 编译检查通过；原合同文件、SourceAuthority、source_recovery、consolidation_sources 未改。未运行完整产品/浏览器/真实模型/QQ。生产 PostgreSQL、镜像、停写迁移和部署证据另记协调仓库本次发布收据，不用本地结果冒称已上线。
