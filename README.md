# PRISM（棱镜）

> 一个面向政策、学术论争与公共议题的开源演变追踪系统。
>
> PRISM 不是“新闻摘要器”。新闻、论文和政策文件只是观测材料；系统真正追踪的是一个对象如何被提出、解释、争论、修订、替代或判定失效，以及每一步判断依赖什么证据。

PRISM 以可审计的 Markdown 语料为事实材料正本，以 SQLite/FTS5 建立可重建的文本索引，并通过 Graphiti/GTI 的可选时序图后端表达事实的有效期、观察时间、修订与冲突。分析与辩论始终建立在同一份历史快照和证据集合之上，不把最新材料无标记地倒灌到过去，也不把时间上的先后自动解释为因果。

## 当前能力

- 不可变、经过验证的领域模型，以及支持 `PRISM_HOME` 的可移植配置；
- Markdown/PDF/OCR 摄入、原始文件留存、SQLite/FTS5 索引与条件检索；
- 自动事件驱动管线：每次摄入都会执行 index → extract → accumulated-case merge → graph write，无需再手工串联管线；
- 持久化的案例抽取账本，可在重启后仅依靠本地 PRISM 数据重建累计案例；
- 默认不依赖 Graphiti/GTI、需要时才启用的时序图适配器与历史时间线契约；
- M1 的带来源变化关系（`supersedes`、`revises`、`contradicts`、`triggered_by`）、失效事实审计和双截止点比较；
- M2 的自动多视角解释、单轮交叉质询、证据约束综合与持久化审计；
- M3 的正式历史快照（`snapshot`/`query_historical_snapshot`）、fail-closed 知识边界、确定性阶段过滤（`--stage`）和双时点比较（`compare`/`compare_case_history`）；
- 不可变报告版本、可选 PDF 导出、指定视角追问，以及追加材料与既有辩论上下文的确定性关联；
- 可选 NiceGUI 案例主页：用 Plotly 展示历史时间线，并通过可点击节点查看带来源的证据定位信息；
- 默认完全离线、显式选择后才启用的 Graphiti/Neo4j spike 配置、部署模板和 live-test gate；
- 所有已完成模块均有离线测试覆盖。

语料目录中的 Markdown 文件是可读的材料正本。SQLite 是可重建的文本索引，同时承载项目自有的持久化账本；Graphiti/GTI 是可选的时序图后端。默认测试不需要真实 provider 凭据或外部服务。

## 核心数据流

```text
Markdown / PDF / OCR / 来源抓取
        │
        ├── 原始输入留存到 raw/
        ▼
标准化 Markdown + frontmatter → corpus/
        │
        └── material.ingested
                ▼
        SQLite/FTS5 索引
                ▼
        LLM extract + 确定性证据校验
                ▼
        case_extraction_ledger 累计案例合并
                ▼
        graph write（默认离线后端；Graphiti/GTI 可选）
                ▼
        时间线 / 历史快照 / 比较 / 辩论 / 版本化报告
```

时间在模型中分为三个维度：事件发生时间 `happened_at`、事实有效时间 `valid_at`/`invalid_at`，以及观察或发布时间 `observed_at`。历史查询同时应用有效时间和观察/发布时间边界，因此后来的回顾性材料不会泄漏到更早的截止点。

## 安装

PRISM 要求 Python `>=3.11`。核心安装的默认 `dependencies` 为空：基础运行时不因安装而引入 Web 框架、PDF 工具或 Graphiti/Neo4j 客户端，也不会默认打开网络连接。

| 用途 | 安装命令 | 边界 |
|---|---|---|
| 核心 | `pip install -e .` | 默认依赖为空，保持离线 |
| 开发与离线测试 | `pip install -e ".[dev]"` | 仅加入 pytest |
| PDF 导出 | `pip install -e ".[pdf]"` | 可选的 Python-Markdown 与 pypdf |
| WebUI | `pip install -e ".[webui]"` | 可选的 NiceGUI 与 Plotly |
| Graphiti/Neo4j | `pip install -e ".[graphiti]"` | 可选且固定为 live spike 已验证的版本 |

运行离线测试套件：

```console
python -m pytest -q
```

## 离线 CLI

通过 `PRISM_HOME` 选择本地数据目录。默认运行时不会创建网络客户端：

```console
python -m prism.cli ingest input.md            # ingest + index; automatic processing is queued and finishes before exit
python -m prism.cli ingest input.md --process  # ingest + full automatic pipeline, printing its real outcome
python -m prism.cli process MATERIAL_ID        # synchronous pipeline run (or explicit idempotent replay)
python -m prism.cli merge-case CASE_ID         # rebuild and write the accumulated case from the durable ledger
python -m prism.cli cases                     # list all accumulated cases from the local ledger
python -m prism.cli add-material INPUT --case-id CASE_ID
python -m prism.cli report CASE_ID --save     # render and persist an immutable report version
python -m prism.cli report-versions CASE_ID --as-of TIMESTAMP # list/filter versions
python -m prism.cli report-version VERSION_ID # read one saved report version
python -m prism.cli rebuild-report CASE_ID    # recompute and version the report
python -m prism.cli discover MATERIAL_ID
python -m prism.cli state CASE_ID --cutoff-at 2026-09-01T00:00:00+00:00
python -m prism.cli timeline CASE_ID --as-of 2026-09-01T00:00:00+00:00
python -m prism.cli snapshot CASE_ID --as-of 2026-02-02T00:00:00+00:00 --stage publication
python -m prism.cli compare CASE_ID --earlier 2026-02-02T00:00:00+00:00 --later 2026-03-12T00:00:00+00:00
python -m prism.cli report CASE_ID --as-of 2026-09-01T00:00:00+00:00 --no-llm
```

每次 `ingest` 都会在事件总线上发布材料，因此在自动管线接好后不存在“只索引、不处理”的摄入。未带 `--process` 时，命令先打印摄入结果，但仍会等待排队的处理完成后才退出；如果处理失败，命令会以非零状态退出并保留可审计的失败记录。`ingest --process` 和 `process` 是同步入口，只有在材料管线及累计案例结果产生后才返回；对本进程中已处理材料再次执行 `process` 是显式幂等重放，并返回 `"replayed": true`，不会重复合并。

### 自动管线的状态与故障语义

`PrismAPI.ingest_material` 或来源抓取发布 `material.ingested` 后，运行时订阅者会把材料交给 `PipelineService.handle_event`，并依次执行 index → extract → case merge → graph write。订阅在事件总线启动前注册，在运行时关闭时等待正在执行的事件排空后再移除。

- `ingest_material` 是异步、事件驱动入口：材料完成摄入和索引、自动处理进入队列后即可返回，不宣称管线已经完成。
- `pipeline.outcome_for(MATERIAL_ID)` 返回生命周期状态：执行中为 `pending`，失败后为 `failed`，全部阶段成功后才是 `committed`。
- `pipeline.run_for` 返回已完成运行；`pipeline.failure_for(material_id)` 返回最近一次失败的阶段、错误类型和时间。
- `process_material(MATERIAL_ID)` 等待正在执行或已经完成的运行。失败材料可安全重试；持续失败会抛出结构化 `PipelineError`，包含 stage、material id 和 completed stages。
- `process_material(PATH)` 自行同步执行管线，再发布材料事件；其 `case_outcome` 直接来自管线中的案例记录器，不会追加一次无意义的二次合并。

`pipeline.run_for` 与 `pipeline.failure_for` 分别提供完成运行和最近失败审计；同步结果用 `ProcessMaterialResult.replayed` 明确区分进程内幂等重放。

已完成运行的注册表只在当前进程内有效。新 CLI 进程面对已持久化为 `committed` 的材料仍会真实重跑，所以该次结果是 `replayed: false`。写入本身保持幂等：图 episode 按 `episode_key` 去重，账本行在同一案例内 upsert；但抽取会重新执行。若新抽取试图把已经绑定的材料改绑到另一案例，`MaterialCaseConflict` 会在任何行或图写入之前拒绝操作，并把失败记为 `error_type: MaterialCaseConflict`。

订阅者失败不会伪装成成功。失败会同时记录为 `PrismRuntime.dispatch_errors` 中带时间戳的 `DispatchError`、`pipeline.failure_for(material_id)` 可查的 `PipelineFailure`，以及材料的 `failed` 生命周期结果。只有 graph/ledger write 在内的全部阶段成功后，运行才会记为完成；成功重试会清除过期失败审计。

终态 `failed`/`committed` 持久化在同一 `index.db` 的 `pipeline_outcomes` 表中，每份材料保留一条当前记录并在下次启动时恢复。`pending` 只存在于运行中的进程，因此崩溃后不会留下陈旧的 `pending` 行。这是本地、单进程文件账本，不是跨进程 outbox；可跨重启审计结果，但不会跨进程派发工作。

每份材料只能绑定一个案例。自动累计器把成功抽取保存在 `case_extraction_ledger`，每次向图写入的是整个累计案例的合并结果，而不是用单份材料覆盖完整案例。`CaseBundleMerger` 对未知来源、标识符冲突和外部案例 id 保持保守拒绝；冲突候选不会自动消失。合并或图写入失败时，新账本项回滚。重启后可仅依靠本地账本重建相同累计案例，无需 LLM、网络或重新抽取。旧账本若已经存在一份材料对应多个案例，仍可通过 `case_ids_for_material` 读取和报告；`case_for_material` 会抛出相同的类型化冲突，而不是意外的 `ValueError`。

`abstract_only`、`metadata_only` 和 `blocked` 材料只进入索引，不参与抽取和图写入；阶段跳过记录会写明访问级别。抽取警告、证据缺口和未解决冲突会原样进入账本，并出现在 `ProcessMaterialResult.warnings` 与 `CaseWriteOutcome.warnings` 中。

统一入口 `PrismAPI.process_material(MATERIAL_ID_OR_PATH)` 返回管线运行、该运行产生的累计案例合并/写入结果及审计警告。`PrismAPI.merge_case(CASE_ID, materials=[...])` 是显式对账入口，可从持久账本或指定材料子集重建完整累计案例，并按 episode key 幂等去重。CLI 的 `ingest --process`、`process`、`merge-case` 使用同一 API。

PRISM 在候选层面是 **LLM-automatic**：系统自动比较证据、处理或保留冲突、在提供目标案例时完成绑定，并记录推理与不确定性。用户负责添加材料、提出问题、选择目标案例或请求重建，无需逐项审批候选。确定性校验仍会拒绝不受支持的引文、时间戳、跨材料引用和不安全模型输出。

## M1：时序演变核心

PRISM 将变化表示为带证据的时序数据，而不是“后写覆盖先写”的最终状态。`TemporalFact.fact_id` 是可选的稳定逻辑引用；后续对同一 id 的观察可以关闭原有效期，但不会删除早先的图 episode。`TemporalRelation` 分别记录自身的有效时间、观察时间、来源、置信度、provenance 和可移植证据定位，并支持 `supersedes`、`revises`、`contradicts`、`triggered_by`。

`GraphService.timeline(case_id, as_of)` 把当前有效状态放在 `entries`，把已知但不再有效的历史放在 `invalidated_entries`。事实超过 `invalid_at` 后不再出现在有效状态中，但仍可追踪；替代事实从自己的 `valid_at` 与观察边界开始可见。不同 `fact_id` 或不同来源的观察不会仅因 subject 与 predicate 相同而被合并，因此相互矛盾的事实可以并存，并各自保留来源、证据、置信度和 provenance。analyzer 的 `compare`、`state`、`analyze` 视图会呈现截止点差异、转折点、失效事实、关系和未决问题。

因果判断刻意比时间顺序更严格。修订、替代或失效只能证明“记录到变化”，不能证明变化原因。`AnalyzerService` 只有在存在带已验证证据的显式 `triggered_by` 关系时，才输出变化原因；旧 payload 的兼容投影除外。否则 M1 视图会加入 `unconfirmed_change_cause` 未决问题。可选 LLM 摘要仅在所有 episode/source 引用都存在于分析结果时才被接受；格式错误或不可验证的输出会回退到确定性、非因果摘要。

抽取和累计过程会把 `invalid_at`、claim 的 `revised_by`、显式关系与未解决冲突一路保留到图写入。完整离线验收范围和 live-service 边界见[《M1 时序核心验收边界》](docs/m1-temporal.md)。

### Evolution Extraction v0

`ExtractionService.extract_material(material, corpus_path=...)` 是管线使用的公开、证据约束抽取入口。它把规范化 Markdown 正文交给 LLM Router 的 `extract` 角色，严格校验 JSON，再逐条确认候选引文确实存在于语料文本中，之后才生成可写图的领域对象。定位失败会保留为显式 extraction evidence gap，绝不会伪造或静默写入图。配置了 extraction service 时，`PrismAPI.extract_material(...)` 暴露同一操作。

预测始终是带不确定性的 claim，不会提升为已经确认的 `TemporalFact`；相互矛盾的候选保留为可报告的 conflict audit item 与图关系。确定性报告将“文档发布”与实质演变分开计数，没有受支持变化的材料不会生成填充用 publication node。

## M2：自动多视角辩论

CLI/API 可以对同一历史案例快照进行自动多视角解释。学术案例使用 `experimental_methods`、`mechanism_explanation`、`evidence_quality`、`research_history`；政策和公共议题使用通用观察视角。所有视角读取同一 EvidenceBundle，输出带类型与引文的陈述，完成一轮自动交叉质询，再综合为共识、分歧、分歧来源、关键证据、未决问题和证伪条件。辩论解释与结构化时间线事实始终分层保存。

```console
python -m prism.cli debate CASE_ID \
  --question "What changed, and why do the interpretations differ?" \
  --as-of 2026-09-04T00:00:00+00:00
```

M2 已在 2026-09-04 使用真实配置的 debate provider 完成两次 smoke：

| 案例 | 真实验收结果 |
|---|---|
| `academic-hnad-evolution` | 4 个学术视角均可用，4 次交叉质询全部完成；综合产生 1 项共识、1 项分歧、1 项分歧来源、1 项关键证据、1 个未决问题、1 个证伪条件 |
| `china-housing-provident-fund` | 4 个通用视角均可用，4 次交叉质询全部完成；综合产生 2 项共识、2 项分歧、2 项分歧来源、3 项关键证据、2 个未决问题、3 个证伪条件 |

这些结果使用已有 PRISM corpus/ledger 数据和真实 Graphiti-backed 案例时间线；它们是 smoke 结果，不保证未来每次 provider 输出都符合相同 JSON 形状。输出漂移会被隔离或保守降级，默认离线运行时不会调用真实 provider。详细契约见[《M2 自动辩论验收》](docs/m2-debate.md)。

尚未完成的 M2 范围包括多轮辩论、运行中由用户定向中断，以及 NiceGUI 辩论剧场。

## M3：历史查询、报告与交互切片

### 指定视角追问

M3 支持在已有辩论运行上进行指定视角追问：

```console
python -m prism.cli follow-up PARENT_RUN_ID \
  --perspective institutional_regulatory \
  --question "Why did implementation begin at this point?"
```

追问复用父运行的案例、历史截止点和 evidence-bundle hash，并以父链接单独持久化。操作是幂等的；证据快照发生变化时会拒绝静默复用。这是 API/CLI 切片，尚不是 NiceGUI 辩论剧场。

### 追加材料并关联辩论上下文

```console
python -m prism.cli add-material INPUT.md \
  --case-id CASE_ID \
  --parent-debate-run PARENT_RUN_ID
```

PRISM 会先验证持久化的父运行，再通过现有管线处理材料，然后在父运行截止点重新计算 GTI/analyzer evidence-bundle hash；此过程不会调用 debate LLM。结果会给出先前/当前 hash 和父运行是否 `stale`。快照改变时不会静默复用旧辩论，也不会自动重新辩论。成功追加会在同一截止点创建通常的不可变 `material_added` 报告版本。详见[《M3 材料追加与辩论上下文关联》](docs/m3-material-debate-link.md)。

### NiceGUI 案例主页

可选的 NiceGUI 案例主页用于浏览累计案例，并加载与 CLI 相同的 GTI-backed 历史快照：

```console
pip install -e ".[webui]"
python -m prism.webui
```

服务绑定 `127.0.0.1`，不自动打开浏览器；当前 runner 拒绝非 loopback host。它是一个薄 facade client：Plotly 时间线绘制 `PrismAPI.query_historical_snapshot` 返回的每个有效或已失效条目，stage/kind/cutoff 都是 facade 输入，而非浏览器端的时序过滤。有效条目与已失效事实采用不同 marker 和 label；点击节点时，用稳定的 `episode_key` 在当前快照中查找同一条目，直接显示 `source_ids`、corpus path、段落/页码和 quote，不发起第二次 API 读取。

NiceGUI 与 Plotly 都是 `webui` extra 中延迟导入的可选依赖；导入 controller 不需要安装两者。当前 WebUI 不含辩论剧场、实时流、暂停/继续、证据上传或 corpus 浏览器、模型设置、多用户认证和远程暴露。完整边界见[《M3 NiceGUI 案例主页与历史时间线》](docs/m3-webui-case-home.md)。

### 报告版本与 PDF

`cases` 只读取项目自有的持久化案例账本，不读取 Graphiti，返回案例标识、材料数量、证据观察时间范围、最新节点时间、更新时间，以及是否仍有证据缺口或冲突。

报告版本存放在同一 `index.db` 的增量表 `report_versions` 中。每行包含稳定 version id、`as_of`、输入和 Markdown hash、摘要来源、可选 debate input hash、父版本、trigger 和渲染后的 Markdown。记录不可变；相同的分析/辩论输入会复用已有版本，不会再次调用 report LLM。`add-material` 只有在 extraction/merge/graph 全部成功后才写入 `material_added` 版本；抽取或跨案例绑定失败不会产生版本。报告把 debate interpretation 放在独立章节，结构化事实仍以分析结果为准。

已有报告版本可通过 `report-version --pdf` 导出到项目相对 PDF 路径：

```console
python -m prism.cli report-version rv_... --pdf reports/case-b.pdf
```

PDF 是派生的交付物，不是报告正本。导出不会创建或修改报告版本；相同版本和相同 bytes 的导出是幂等的，若目标已有不同文件则拒绝覆盖。

安装可选依赖：

```console
pip install -e ".[pdf]"
```

导出流程使用 Python-Markdown 渲染 Markdown，再交给 headless Microsoft Edge 或兼容 Chromium 浏览器打印，最后用 pypdf 验证页数和提取文本。自动发现不适用时，可把 `PRISM_PDF_RENDERER` 指向 Edge/Chromium 可执行文件。缺少 Python 依赖或 renderer 时，导出会明确失败且不创建 PDF。

### 正式历史快照与比较

`snapshot` 与 `compare` 把 FR-3.6/FR-4.2/FR-4.3 的历史查询正式化为现有 graph + analyzer 栈上的增量 facade 入口，不另建平行的事实或快照存储：

```console
python -m prism.cli snapshot CASE_ID --as-of 2026-02-02T00:00:00+00:00
python -m prism.cli snapshot CASE_ID --as-of 2026-02-02T00:00:00+00:00 --stage publication
python -m prism.cli compare CASE_ID --earlier 2026-02-02T00:00:00+00:00 --later 2026-03-12T00:00:00+00:00
```

`PrismAPI.query_historical_snapshot` 由 `AnalyzerService.snapshot` 支撑，在一个 timezone-aware 时点返回可审计的 `HistoricalCaseState`：有效节点、事实、claim、关系、截至该时点已失效的事实，以及案例证据缺口。

知识边界执行两次：`GraphService.timeline` 只返回 `reference_time`（观察/发布时间）不晚于截止点的条目；`snapshot` 对仅在截止点之后才被知道、有效窗口不匹配，或仍有效却被错误标成失效的条目 fail-closed。`PrismAPI.compare_case_history` 委托现有 `AnalyzerService.compare`，返回 `EvolutionComparison` 的 added/removed/unchanged，并拒绝 naive 或先后颠倒的时点。

`--stage` 只按图中已经记录的 marker 做确定性查询。固定词汇表为 `prism.analyzer.STAGES`：例如 `evolution_node.node_type` 的 `publication`/`revision`/`expiry`（FR-4.5 政策链与 FR-4.6 论争链位置），以及 `claim.stance` 的 `support`/`oppose`。LLM 不参与归类；未知 stage 会在读图前被拒绝。过滤结果保留其 kind 对应的 layer、来源和可移植证据定位。可重复的 `--kind` 与 `--stage` 组合使用。旧入口 `timeline`、`state`、`build_timeline`、`query_history`、`query_case_state` 保持不变。详见[《M3 历史快照、阶段过滤与比较验收》](docs/m3-historical-snapshot.md)。

## 研究发现与学术证据等级

### 受控研究发现

`discover` 从已索引材料生成有时间边界的研究计划。执行计划前，必须在 `PRISM_HOME/config.json` 中显式启用 Firecrawl，并通过配置指定的环境变量提供密钥：

```json
{
  "sources": {"whitelist": ["example.gov"]},
  "firecrawl": {
    "enabled": true,
    "api_key_env": "FIRECRAWL_API_KEY",
    "base_url": "https://api.firecrawl.dev",
    "limit": 10,
    "timeout": 10
  }
}
```

```console
# POSIX shell
export FIRECRAWL_API_KEY="..."
python -m prism.cli research MATERIAL_ID
python -m prism.cli research MATERIAL_ID --no-process
```

密钥本身不得写入 JSON。Firecrawl 返回的只是发现线索；`research` 会先通过 PRISM 的白名单来源服务重新抓取每个公开 URL，再摄入结果。

长报告的研究规划器可以抽取政策动作、指标、机制、主体、预测等可检索概念。每个概念形成独立、可审计的查询，目标结果数为 10–20，默认 10、最高可配置到 20。查询携带 `concept_id`，按该上限执行 Firecrawl Search，并在权威重抓前对 URL 做全局去重。来源不足、URL 重复、超时或正文抽取失败时，结果可以少于目标数；系统记录真实结果，不用重复项补足数量。

### 学术证据等级

PRISM 不限定学科。学术候选可以通过 Crossref、OpenAlex 等公开 DOI 元数据服务解析，并以可选适配器扩展领域索引。证据等级明确标注：

| 等级 | 含义 |
|---|---|
| `fulltext` | 已采集公开文章正文 |
| `abstract_only` | 取得公开摘要，但没有文章正文 |
| `metadata_only` | 仅取得 title/author/venue/DOI 元数据；corpus 记录只是书目占位，不是全文 |
| `blocked` | 访问被拒绝，遇到 paywall/login/captcha，或公开响应无法验证 |

摘要保存在 `summary`，绝不会冒充正文写入 `content`。`authors`、`container_title` 与 `doi` 会从来源项传到 corpus frontmatter、索引记录和搜索结果。带 DOI 的文章页面受阻时，API 可在无需凭据的情况下回退到 Crossref/OpenAlex 元数据；OpenAlex 每次 DOI 解析最多查询一次，结果仍保留明确等级与追踪信息。

当 Crossref 只返回元数据而无摘要时，resolver 会请求一次 OpenAlex enrichment。该过程只合并、不替换：Crossref 对 `title`、`doi`、`authors`、`container_title`、`link`、`published_at` 保持权威，OpenAlex 只补充摘要和 Crossref 缺失的字段，例如在 Crossref 缺少 `container_title` 时采用 `primary_location.source.display_name`。OpenAlex 没有摘要或 enrichment 失败时，原有 Crossref `metadata_only` 记录保持不变。

`blocked` 的识别刻意保守：只有 wall 提示出现在可见文本前 400 个字符内，且全部可见文本少于 2000 个字符时，响应才被视为 CAPTCHA、“checking your browser”等访问验证墙。长页面只是讨论或引用这些字样时不会被错误标记。

## Graphiti/GTI 可选后端

PRISM 的图层面向项目自有的 Graphiti/Neo4j 实例（FR-3）。默认运行时不会导入 `graphiti-core`/`neo4j`、构造客户端、探测可选包或读取 Graphiti 凭据。只有同时安装 `[graphiti]` extra 并启用配置后，才进入真实后端路径：

```console
pip install -e ".[graphiti]"
```

```json
{
  "graphiti": {
    "enabled": true,
    "uri": "bolt://localhost:7688",
    "database": "neo4j",
    "group_id": "neo4j",
    "username_env": "",
    "password_env": "PRISM_GRAPHITI_PASSWORD",
    "timeout": 30.0
  }
}
```

此示例连接 PRISM 自有容器中唯一的内置 `neo4j` 数据库。模板使用 Neo4j Community Edition，服务名为 `prism-graphiti-spike`，不设置自定义默认数据库。对 `graphiti-core==0.29.3` 而言，显式 `group_id` 会被当作数据库选择：`add_episode` 会在两者不同时选择 `database=group_id`。因此 `enabled: true` 时 `database` 与 `group_id` 必须相同，此处都为 `neo4j`。PRISM 会在构造客户端前拒绝两者不一致的配置。隔离来自独立的 PRISM 自有容器、Neo4j home、服务和数据卷，不来自 Community 实例中的多个 group；Community Edition 不支持这种多数据库隔离。

`database` 是 PRISM adapter metadata，`Graphiti(uri, user, password, ...)` 构造函数不会消费它；它必须与 Graphiti 实际选库使用的 `group_id` 相等。`graphiti.uri` 必须显式携带非默认端口，标准 7474/7687 不会被自动套用，从而避免误连默认本地 Neo4j。

`graphiti.uri` 不得内嵌凭据。配置只保存环境变量名称（`PRISM_GRAPHITI_PASSWORD`，以及可选的 `PRISM_GRAPHITI_USERNAME`），不保存值。启用后，缺失凭据或可选包会在接触服务前明确失败。

真实路径会通过 `src/prism/graph/registry.py` 在现有 `index.db` 中增量创建 `graphiti_episode_registry`，记录 PRISM `episode_key`、Graphiti 分配的真实 uuid、group/database 和 canonical episode body，不存凭据、host 或绝对路径。该持久映射使跨进程重启的重复写入保持 no-op，并让不带 body 的 `EntityEdge` 搜索结果仍能正确归属。`PrismRuntime.close()` 会关闭它创建的 backend 与 registry；调用方也可向 `create_runtime` 注入 `graph_backend`/`graphiti_client_factory`，其中调用方注入的 `graph_backend` 是完全覆盖，不会另建 registry。

项目提供 `deploy/graphiti-spike/` 部署模板。运行前可执行端口预检，但预检不构成端口保留：

```console
python deploy/graphiti-spike/check_ports.py
docker compose -f deploy/graphiti-spike/compose.yaml up -d
```

### Phase B 真实验收记录

Phase A 已完成代码、配置、部署模板和离线测试。Phase B 于 2026-09-03 在隔离、仅 loopback 可访问的 PRISM 自有 Neo4j Community 5.26 server 上执行并通过 3 个 opt-in integration tests：HTTP `127.0.0.1:7475` / Bolt `127.0.0.1:7688`，环境为 Python 3.12、`graphiti-core==0.29.3`、`neo4j==6.3.0`、`httpx==0.28.1`。

3 个测试覆盖真实 Neo4j/Graphiti 的写入、读取、重启后幂等重写、历史截止点、关系与 registry 行为。测试注入确定性的 Graphiti LLM/embedder/reranker clients，`OPENAI_API_KEY` 不存在，没有调用外部 LLM、embedding 或 rerank provider；使用的是带随机后缀的合成 fixture，不是真实 corpus 案例。

真实 `graphiti-core==0.29.3` 的 `search` 默认窗口是整个 group 的 10 个结果；当前 adapter 强制使用 100 个结果，作为有界的 spike safeguard，并不是分页。已测试的 3 个案例各自不超过 8 个 episodes，在当时累计图规模下完整返回。超过约 100 个 entity edges 的案例或累计 group 仍需要正式分页设计。

未验收范围包括：真实 provider 的 extraction、真实 entity/edge 图质量、针对真实政策/新闻 corpus 的端到端重跑、生产规模分页，以及 Docker Compose/healthcheck 变体本身（执行环境未安装 Docker，实际使用的是独立的 native Neo4j launcher）。

opt-in live integration tests 只有在环境中同时设置 `PRISM_GRAPHITI_URI` 与 `PRISM_GRAPHITI_PASSWORD` 时才运行，不属于默认 CI：

```console
python -m pytest tests/test_graphiti_integration.py -v
```

完整计划、side effects、rollback 与验收 API surface 见[《Graphiti/Neo4j Spike 阶段计划》](docs/graphiti-spike-plan.md)。

## 里程碑状态与诚实边界

| 里程碑 | 已完成 | 尚未完成或未验收 |
|---|---|---|
| M0 基础能力 | 领域模型、可移植配置、Markdown/PDF/OCR 摄入、raw 留存、SQLite/FTS5、自动管线、持久案例账本与离线测试 | 需求基线中的真实单案例闭环尚无完整验收记录；仓库测试使用合成 fixture，README 不据此宣称已经完成真实政策/新闻案例的完整语义验收 |
| M1 时序核心 | 时序事实/关系、事实失效与修订、冲突并存、历史截止点、双截止点比较、证据定位；合成数据的 Graphiti live round-trip 已通过 | 真实 LLM extraction、真实 entity/edge 质量、真实案例端到端重跑仍未验收 |
| M2 自动辩论 | 3–4 个视角、同一 EvidenceBundle、陈述分类、单轮交叉质询、证据约束综合、SQLite 审计与两类真实 provider smoke | 多轮辩论、运行中用户中断、NiceGUI 辩论剧场仍未完成 |
| M3 产品切片 | `snapshot`/`compare`、`--stage`/`--kind`、报告版本与 PDF、指定视角追问、材料/父辩论关联、NiceGUI 案例主页与 Plotly 时间线 | 辩论中的阶段重建、证据上传/浏览、模型设置、认证、远程暴露和完整多并行案例交互仍未完成 |

## 环境变量

| 名称 | 用途 |
|---|---|
| `PRISM_HOME` | 本地数据与配置目录 |
| `FIRECRAWL_API_KEY` | 显式启用 Firecrawl research 时使用 |
| `PRISM_PDF_RENDERER` | 指定 Edge/Chromium PDF renderer |
| `PRISM_GRAPHITI_URI` | opt-in Graphiti integration tests 的 `bolt://host:port` |
| `PRISM_GRAPHITI_PASSWORD` | Graphiti runtime、tests 与 compose 使用的密码 |
| `PRISM_GRAPHITI_USERNAME` | 仅在 `graphiti.username_env` 被设置时使用；否则采用 `neo4j` |
| `PRISM_GRAPHITI_DATABASE` | 可选数据库名；Community 模板默认为 `neo4j` |
| `PRISM_GRAPHITI_GROUP` | 可选 group；默认跟随 `PRISM_GRAPHITI_DATABASE` 且必须与其相同 |
| `PRISM_GRAPHITI_HTTP_PORT` / `PRISM_GRAPHITI_BOLT_PORT` | compose 发布的 host ports，默认 7475/7688 |
| `GRAPHITI_TELEMETRY_ENABLED` | Graphiti 0.29.3 telemetry 开关；PRISM builder 默认设为 `false`，除非 operator 已显式导出 `true` |

真实 API key、Cookie 或密码不得写入配置、corpus、图谱、日志、报告或仓库。

## 许可证与依赖边界

PRISM 的目标许可证为 **MIT**；`LICENSE`、`CONTRIBUTING.md`、`CHANGELOG.md` 与 `CODE_OF_CONDUCT.md` 属于正式发布前仍待补齐的治理文件。核心包的默认依赖列表为空；`pdf`、`webui`、`graphiti` 都是显式选择的 optional extra：

- `pdf` 固定使用 `markdown==3.10.2`、`pypdf==6.16.1`，并依赖系统中已安装的 Microsoft Edge 或兼容 Chromium；
- `webui` 使用 `nicegui==2.24.2`、`plotly>=5.24,<7`，不会因导入核心模块而启动 Web 框架或监听端口；
- `graphiti` 固定使用 `graphiti-core==0.29.3`、`neo4j==6.3.0`、`httpx==0.28.1`，默认安装与默认运行路径不会接触它们；
- PDF 摄入/OCR 的选型边界要求使用与 MIT 兼容的许可；当前设计采用 pdfplumber 与 RapidOCR，避免 AGPL 依赖边界。

## 进一步阅读

- [M1 时序核心验收边界](docs/m1-temporal.md)
- [M2 自动辩论验收](docs/m2-debate.md)
- [M3 材料追加与辩论上下文关联](docs/m3-material-debate-link.md)
- [M3 NiceGUI 案例主页与历史时间线](docs/m3-webui-case-home.md)
- [M3 历史快照、阶段过滤与比较验收](docs/m3-historical-snapshot.md)
- [Graphiti/Neo4j Spike 阶段计划](docs/graphiti-spike-plan.md)
