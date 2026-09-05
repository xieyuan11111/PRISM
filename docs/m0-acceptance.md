# M0 单案例演变闭环验收（当前状态）

> 本文档区分仓库内机制验收与项目外真实案例验收。真实材料与正式报告不进入 Git；项目外验收结果只记录脱敏结论，不以离线演示冒充真实语义通过。

## 验收目标

PRISM 的对象是政策、观点或公共议题随时间的变化。M0 不以文章数量或摘要长度为通过条件，而验证：时序状态正确、节点有实质变化含义、结论可回到 corpus 原文，证据不足时产生显式 evidence gap。

## 已完成的仓库内机制

- `EvidenceLocator` 只接受项目相对 corpus 路径，携带段落号/页码/原文片段锚点；拒绝绝对路径、盘符路径和 `..` 逃逸。
- `EvidenceStore.locate()` 只在索引中存在原文时生成定位；source 不存在、段落越界或 quote 不在原文时显式失败。
- 领域模型支持 `valid_at`、`invalid_at`、`observed_at`、`evidence`、`provenance_type`、`change_reason`，时间强制 timezone-aware，证据规范化为 tuple。
- 图谱 episode 使用 `prism.graph.episode.v2`，历史查询同时约束有效时间和观察/发布时间，防止后来材料污染早期状态。
- `AnalyzerService.state()`、历史快照、CLI/API cutoff 与 compare 均由确定性层执行，未来有效或未来才可观察的内容不会倒灌历史。
- 报告确定性路径包含事实/解释分层、时间轴、provenance、可定位引用和 evidence gap；不会把材料出处行或 publication-only 记录凑成演变节点。
- 自动 pipeline 支持材料摄入、LLM 抽取、案例累计与图写入；失败被保留为审计记录或 gap。

## 真实验收状态

2026-09-05 已在项目外使用真实政策材料、真实 provider 与 PRISM-owned 原生 Neo4j/Graphiti 完成一次端到端机制验收：

```text
真实 provider → 真实材料 → extraction → case ledger → Graphiti write
→ fresh runtime/readback → historical cutoff → report version → PDF
```

结果分层为：

```text
mechanism_status = pass
semantic_status = partial
```

这证明真实生产链路可以运行，不证明真实 LLM 已稳定重建完整政策生命周期。

## M0 当前清单

| 项目 | 当前状态 | 证据与边界 |
|---|---|---|
| 单案例机制闭环 | implemented | 离线测试与真实窄案例机制链均已运行 |
| 2–3 个合规真实来源 | semantic-partial / project-external | 真实案例材料在项目外使用过；仓库不携带真实 corpus |
| Markdown + frontmatter | implemented | 摄入、规范化与拒绝规则有测试 |
| SQLite / FTS5 | implemented | 索引、搜索与证据定位有测试 |
| 独立 Graphiti / Neo4j 最小链 | live-mechanism-pass | 原生 Neo4j、真实 Graphiti 写入/读回/重启/cutoff 已验证 |
| 至少 5 个真实实质演变节点 | not-yet-verified | 不用节点数量凑验收；真实语义仍需完整案例抽样 |
| 截至某日状态 | implemented / live-mechanism-pass | cutoff、未来隔离、历史快照已验证 |
| 带证据演变报告 | implemented / live-mechanism-pass | 报告版本与 PDF 已真实运行；语义仍单独判定 |

## 当前真实 LLM 边界

- 真实 provider 可能输出候选级校验 gap、改写 quote、时间不合规或 JSON envelope failure。
- accepted records 的 source/evidence coverage 可以达到 100%，但不能代替跨运行语义稳定性。
- Flash 的 `protocol-v2 + split-v1` 在一个窄政策材料上三轮保持核心 revision node 与 6 条 temporal facts 的 ID 交集；claims、relations 与跨案例泛化仍未完成。
- 不放宽 quote、时间、source、case 或 relation 规则；不人工生成节点或关系。

## 未完成与下一步

```text
1. 第二个政策案例 + 一个学术案例的 split-v1 三轮复现
2. 一个具备真实材料链的完整生命周期案例验收
3. 文档、治理、CI、lint、依赖和安全收口
4. WebUI 最终本机真实冒烟
```

Docker / Docker Compose 不属于 PRISM 路线，不作为 M0/M1、安装、CI 或 Graphiti 验收前置。Graphiti 使用原生 Neo4j launcher 与独立运行目录。
