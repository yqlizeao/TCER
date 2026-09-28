"""团队概念对齐（Concept Alignment）模块。

设计文档：``doc/concept-alignment-server.md``。纯标准库 + SQLite，挂在现有
``db.connect()`` 的同一个库文件上；模型能力（jev / LLM）走可插拔判定器，未配置
即零外联。

子模块分层（与文档 §3 的 S0–S7 对应）：

- ``schema``      建表 / 迁移
- ``rbac``        最小角色权限 + 本人授权
- ``profile``     成员职能档案、语义分析授权（consent）
- ``disclosure``  **唯一出口**：披露门槛 + 访问审计
- ``governance``  团队术语库（版本 / 评审 / 质疑 / 导入导出 / 上下文术语段）
- ``ingest``      S1：conversation → utterance（说话方分层、剥离注入、脱敏）
- ``lexicon``     S2：已知术语命中 + 新词挖掘（n-gram / G²）
- ``signals``     S3：人↔AI 误解、AI 换词、AI↔AI 保真度、共现
- ``pipeline``    重建调度（增量 / 全量，后台线程）
- ``views``       个人 / 团队 / 管理三视角载荷组装
- ``api``         HTTP 路由
"""
