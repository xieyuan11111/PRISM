# 真实 Graphiti 窄案例验收 Runner

`tools/run_live_case_acceptance.py` 是 PRISM Release Candidate 的真实后端
验收 runner。它只在前置条件满足时运行，不会启动或停止 Neo4j/Graphiti，
也不会把 OfflineGraphBackend 当作真实后端。

## 运行范围

runner 针对窄北京住房政策案例读取项目外的真实 corpus，使用配置的真实
LLM provider，并要求 PRISM-owned Graphiti/Neo4j 通过 loopback HTTP/Bolt
可访问。它会检查：

- 材料自动 pipeline、目标案例绑定和 graph write；
- 真实 Graphiti 的 case/node/fact/claim/relation readback；
- source/evidence 与 valid/invalid/reference 时间字段；
- fresh runtime 重启后的 registry/readback；
- 两个历史 cutoff；
- 报告版本与可选 PDF；
- 脱敏的 JSON/Markdown 验收摘要。

输入材料和 provider 凭据只来自命令行参数/环境与项目外工作区，真实正文
不写入仓库。缺少 Graphiti password、loopback 服务或可选依赖时，runner
返回 `BLOCKED`，不会伪造 `PASS`。

## 当前边界

本 runner 已通过离线 contract tests，但本机当前真实运行前置尚未满足：
`PRISM_GRAPHITI_PASSWORD` 未设置，PRISM-owned Neo4j loopback ports 未
监听。因此真实 Graphiti acceptance 当前应记录为 `BLOCKED`，不是通过。
准备好项目自有密码和服务后，再用同一 runner 执行，不需要改产品代码。
