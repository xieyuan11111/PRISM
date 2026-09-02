# M0 单案例演变闭环验收（仓库内可复现结论）

> 本文档只记录本仓库（代码 + 测试 + 合成验收语料）可以直接验证的结论，使用项目相对路径与通用表述。真实案例语料与正式报告产物不在本仓库内，凡涉及真实材料的验收项一律如实标记为未完成，不以离线演示或合成数据冒充。

## 验收目标

PRISM 的分析对象是政策、观点或公共议题随时间的变化；新闻与研究材料只是观测与举证材料。M0 验收不以“收集了多少文章”或“生成了摘要”为通过条件，而是验证：时序状态正确、节点具有实质变化含义、结论可回到 corpus 原文，并且当证据不足时报告显式声明 evidence gap，而不是编造节点或结论。

## 本轮交付（可复现）

- `EvidenceLocator`：只接受项目相对 corpus 路径（拒绝绝对路径、盘符限定路径与 `..` 逃逸），携带段落号/页码/原文片段锚点；不带任何定位锚点的裸路径构造会被拒绝。
- `EvidenceStore.locate()`：仅在索引内真实存在原文时生成定位；找不到 source、段落号越界或引文不在原文中时显式失败（`LookupError`/`ValueError`），杜绝引用 corpus 之外的文本。
- `EvolutionNode` 追加 `valid_at`/`observed_at`/`evidence`/`change_reason`/`provenance_type`，`EvolutionCase` 追加 `status_at`/`status_observed_at`，`TemporalFact`/`Claim` 追加 `evidence`——全部为带默认值的尾部字段，旧的位置参数构造保持不变；frozen + slots 不变；时间一律强制 timezone-aware；证据一律规整为 tuple，并校验其 `source_id` 必须出现在 `source_ids`/`based_on` 中。
- `Claim` 追加独立 `observed_at`（尾部可选字段）：`stated_at` 是主体提出主张的时间，`observed_at` 是该主张经由记录它的材料变得可观察的时间；抽取路径把 `observed_at` 绑定到断言该主张的材料的 `published_at`，合并路径保留该字段，图谱 claim episode 序列化并以其为 knowledge 边界。
- 图谱 episode schema 升为 `prism.graph.episode.v2`；历史查询同时要求 `reference_time`（观察/发布时间）与 `valid_at` 不晚于 cutoff，并沿用 `[valid_at, invalid_at)` 有效区间，防止“后来才被材料观察到的回溯内容”污染早期状态。
- **材料可用时间下界（latest necessary observation）**：`GraphService.add_case` 提交材料时，node/fact/claim 的观察边界不会早于其绑定材料的 `published_at`；引用多个材料时取**最晚**的可用时间（最晚必要观察时间），绝不默认最早时间。这样即使记录里写了过早的 `observed_at`，事实也不会在支持它的材料发布之前进入历史状态。
- **旧合并包兼容加载层**：`prism.cases.LegacyCaseLoader` 正式、可复用加载旧 merged-case JSON（node 缺 `observed_at`/`evidence`、claim 缺 `observed_at`、fact 的 `observed_at` 早于绑定材料等 v1 形态）。加载时优先用绑定材料 `published_at`（缺省才退回 `fetched_at`）保守推导或校正观察时间；无法确定时**不伪造时间**：无法表征的 fact 记录被跳过、node/claim 保持 `observed_at=None`，并以结构化 `LegacyLoadIssue` 显式记录 `observed_at_derived` / `observed_at_bounded_later` / `observed_at_undetermined` 等决策与缺口。多来源记录一律取最晚必要观察时间；`case.status_at` 存在但无 `status_observed_at` 时给出风险 issue。
- `AnalyzerService.state()` / `PrismAPI.query_case_state()` / CLI `state --cutoff-at`：返回截至 cutoff 的案例状态、节点、事实、解释与证据缺口；测试覆盖未来才生效的节点、未来才观察到的回溯节点/事实、cutoff 之后才声明的观点、未来才变更的案例状态均被排除。
- `ReportService` 确定性路径（无 LLM）渲染 Markdown：时间线表含 layer（`fact`/`interpretation`）、发生时间、有效时间、观察时间与 provenance；Citations 区展示 corpus 相对路径、段落/页码与来源原文片段；有 `source_id` 但无定位的条目产生 `missing_evidence_location` 缺口，不会被当作完整证据链；条件性观点以 interpretation 层展示（stance=`conditional`），不会被当作已证实事实。确定性 fallback 摘要按构成如实计数（如“N evolution node(s)（node types: …）、M fact(s)、K claim(s)、G evidence gap(s)”），材料出处行不计为演变节点，不自动凑节点数。
- 缺口分类：`empty_timeline` / `missing_case_definition` / `unattributed_entry` / `missing_evidence_location` 由确定性分析器自动产生；`missing_primary_source` 与 `unverified_prediction` 属于记录型缺口分类（供案例审计方显式记录“缺少一手原文”“预测未经官方确认”），确定性分析器不自动推断，避免把“可能缺材料”当成结论。

## 合成验收语料（离线闭环）

本仓库没有真实政策案例语料。M0 离线演示使用测试内构造的合成语料（`tests/test_m0_evolution.py` 的 `m0-case`：5 个可定位实质节点 + 1 个未来才生效的节点 + 1 条未来才被观察到的回溯事实 + 1 条条件性预测观点），验证：

- cutoff 过滤正确：截至 2026-08-25 的状态恰好包含 5 个实质节点、1 条当时可见的事实与 1 条解释；未来生效、未来观察、未来声明的内容全部排除；
- 报告引文可回到 corpus 相对路径与原文片段；
- 空时间线、无来源条目、有 `source_id` 无定位三种情况都会显式给出 evidence gap。

第二个离线语料（`tests/test_m0_legacy_temporal.py` 的 `late-source-case`）按真实合并包暴露的时间问题形态构造：node/claim 缺 `observed_at`、fact 记录的 `observed_at` 早于绑定材料发布、多来源条目引用晚发布材料。它验证旧包加载层把晚来源记录钉在材料发布日之后，2026-08-20 中间 cutoff 的状态只含当时可观察的记录，晚来源节点/事实/解释/预测全部不进入过去状态；无材料可定位的记录被显式 gap 或跳过，不伪造时间；报告如实计数节点与缺口，不把材料出处行当演变节点。

这些演示证明的是机制，不是“真实材料已满足 5 节点”。若真实材料不足，同一机制会在状态与报告中给出 evidence gap，而不会自动生成节点凑数。

## M0 清单（仓库内状态）

| 项目 | 结果 | 证据与边界 |
| --- | --- | --- |
| 选择单案例 | 机制完成 | 测试与离线演示围绕单一 `m0-case` 演变闭环；真实案例的选择与真实语料验收未在仓库内完成。 |
| 2–3 个合规来源 | 未在仓库内验收 | 测试使用合成来源（占位域名）；未接入真实源、未联网。 |
| Markdown + frontmatter 标准化 | 完成 | 摄入规范化、frontmatter 解析与错误拒绝均有测试覆盖。 |
| SQLite / FTS5 | 完成 | 索引、搜索与证据定位测试覆盖。 |
| 独立 GTI 最小 schema | **未完成** | 仓库提供 v2 episode schema 与依赖注入式 `GraphitiBackend` 适配器，但没有真实独立 Graphiti/Neo4j 实例的持久化写入与查询证据。`OfflineGraphBackend` 只是进程内离线存储，不能冒充真实 GTI；README 与本文档均按“真实 GTI 尚未接入”表述。 |
| 至少 5 个实质节点 | 机制完成（离线） | 合成语料在 cutoff 前可见 5 个实质节点且全部带定位；逐条材料发布不会被机械计为演变节点。真实材料节点数验收仍受“真实语料未入仓库”阻塞。 |
| 截至某日状态 | 完成 | API/CLI 返回状态、节点、事实、解释与 gaps；测试覆盖未来有效、未来观察、未来声明的排除。 |
| 带证据演变报告 | 完成（离线） | 确定性 Markdown 报告含事实/解释分层、provenance、证据缺口与可定位引文；无 LLM、无网络。 |

## 自动化验收（本次实测）

- M0 相关 focused 套件：198 passed（analyzer / api / case-merge / cli / domain / extraction / graph / pipeline / report / runtime / store 与 M0 演变验收、旧合并包时间语义回归文件）。
- 全套离线测试：761 passed + 2 skipped（仅 `tests/test_graphiti_integration.py` 的 opt-in live 用例按设计跳过：未设置 `PRISM_GRAPHITI_URI`/`PRISM_GRAPHITI_PASSWORD`，不会触碰任何真实 Graphiti/Neo4j）；未调用真实网络、LLM、Graphiti 或 Neo4j；运行设置 `PYTHONDONTWRITEBYTECODE=1`。
- 无 `.pyc` 内存编译检查：103 个 Python 文件（src + tests）全部通过，`pyc_written=0`。
- `git diff --check`：通过。

## 未完成与阻塞

1. 真实独立 GTI/Neo4j 尚未接入与验收；`OfflineGraphBackend` 与测试内内存 backend 仅用于离线测试。
2. 真实案例语料未进入本仓库：M0 的“真实材料 ≥ 5 个实质节点”验收需要先以真实、可审计的语料入库，再运行同一 analyzer/report 机制；语料不足时状态与报告会显式给出 evidence gap，不会自动生成节点。旧合并包加载层已就绪，但用真实合并包重放验收需在持有该语料的工作区进行（本仓库不携带任何真实语料）。
3. 真实网页发布时间元数据校正（区分发布时间与抓取时间）依赖真实语料验收，未在本仓库内完成。
4. 已知边界：旧合并包不带 corpus 定位锚点时，加载层无法凭空重建 `EvidenceLocator`（需配合 EvidenceStore/corpus 重定位）；`case.status_at` 存在但无 `status_observed_at` 的旧包记录由加载层给出风险 issue，状态可见性仍锚定在 `status_at`；node/claim 既无记录观察时间又无绑定材料时保持 `observed_at=None` 并给出 issue，图谱按其自身时间锚点回退（风险被显式记录，不伪造时间）。
