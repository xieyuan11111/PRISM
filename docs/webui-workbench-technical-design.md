# PRISM WebUI 工作台技术设计文档

> **版本**：v1.0（待实现评审）  
> **日期**：2026-09-06  
> **适用范围**：PRISM 本机单用户、loopback-only WebUI 迭代  
> **对应需求**：[`docs/webui-workbench-requirements.md`](webui-workbench-requirements.md)

## 1. 设计目标与原则

本设计把当前 WebUI 的“路径追加 + 单次结果显示”升级为可观察的材料工作台：用户在浏览器选择材料后，能够看到材料进入 PRISM、经过各个管线阶段、产生证据与质量状态，并从同一界面打开最终报告、回溯引用和导出 PDF。

必须保持以下系统边界：

1. WebUI 只调用 `PrismAPI` facade；不得直接访问 SQLite、corpus、Graphiti/Neo4j、LLM router 或报告账本。
2. 浏览器上传只是输入适配层；材料仍必须经过既有摄入、索引、抽取、案例合并和图写入管线。
3. Markdown corpus 是材料正本；`raw/` 是原始输入留底；SQLite、图谱和报告账本不替代 corpus。
4. `target_case` 必须由用户显式选择；前端不能按标题、标签、embedding 或模型输出猜测案例。
5. `mechanism_status` 与 `semantic_status` 分开投影；`semantic=partial`、证据缺口或阶段失败不得显示为无条件成功。
6. 报告版本不可变。PDF 只能从已保存的 Markdown 报告版本派生，不能覆盖 Markdown 正本或旧版本。
7. 当前 v1 继续 loopback-only、单用户、无认证、不开启远程绑定；不引入 Docker 作为运行前置。

## 2. 当前实现与缺口

### 2.1 已存在且应复用的契约

| 能力 | 现有接口/记录 | 设计中的用途 |
|---|---|---|
| 材料摄入 | `PrismAPI.ingest_material(path, metadata)` | 仅用于已在 PRISM 进程可见的路径；不作为浏览器上传 API |
| 材料追加与端到端处理 | `PrismAPI.add_material(source, target_case, metadata, as_of, use_llm, parent_debate_run_id)` | 上传落盘后唯一的业务写入口 |
| 同步等待管线 | `PrismAPI.process_material(material_id, ...)` | 需要用户等待结果或重试时的显式操作 |
| 运行记录 | `PipelineRun(material_id, status, stages, started_at, finished_at)` | 投影 index/extract/graph 阶段审计 |
| 终态记录 | `pipeline_outcomes`：`pending/failed/committed` | 重启后的材料终态和失败查询 |
| 报告列表 | `PrismAPI.report_versions(case_id, as_of)` | `/reports` 列表 |
| 报告详情 | `PrismAPI.report_version(version_id)` | `/reports/{version_id}` 正文与元数据显示 |
| PDF | `PrismAPI.export_report_pdf(version_id, output_path)` | 服务器端受控导出后下载 |
| 报告账本 | `ReportVersion`，含 Markdown、hash、case、cutoff、trigger、父版本 | 报告详情、版本链和幂等依据 |
| 现有展示 seam | `MaterialEntryController`、`CaseHomeController`、`status.py` | 新增上传、旅程、报告 controller 时沿用 |

### 2.2 必须新增的 facade 只读能力

现有 `PipelineService` 已有 `run_for()`、`failure_for()`、`outcome_for()`、`outcomes()` 和 `case_outcome_for()`，但 WebUI 不能直接持有该 service。因此 `PrismAPI` 应新增只读 facade 方法，建议命名如下：

```python
async def material_journey(self, material_id: str) -> MaterialJourneyView: ...
async def material_journeys(
    self, *, case_id: str | None = None, status: str | None = None
) -> tuple[MaterialJourneyView, ...]: ...
async def pipeline_run(self, material_id: str) -> PipelineRun | None: ...
async def pipeline_outcome(self, material_id: str) -> PipelineOutcome | None: ...
```

这些方法由 facade 组合材料元数据、`PipelineRun`、`PipelineOutcome`、case outcome、报告版本关联和已保存的分析质量字段，返回只读 DTO 或 `frozen=True, slots=True` dataclass。WebUI 不应把多个底层调用拼装成自己的事实视图。

若需要跨重启显示每个阶段的终态，必须把阶段审计持久化到项目自有 SQLite 表（例如 `pipeline_stage_audits`），或在现有终态记录中增加版本化字段；不能只依赖进程内 `PipelineRun.stages`。新增 schema 必须由 service 初始化、可重建、可迁移，并有旧数据库兼容测试。

## 3. 上传架构

### 3.1 请求链路

```text
NiceGUI ui.upload
  → 上传事件回调
  → UploadController.validate
  → UploadStagingService.stage
  → PrismAPI.add_material(staged_path, target_case, metadata)
  → ingest/raw + normalized corpus Markdown
  → automatic pipeline
  → MaterialJourneyController.refresh
```

`ui.upload` 的回调只处理浏览器传来的文件名、字节流和上传元数据。它不调用 LLM、不写 corpus、不写 SQLite，也不自行创建报告。

### 3.2 staging 规则

建议在 `PRISM_HOME` 下增加项目专用临时目录，例如：

```text
PRISM_HOME/
├── staging/uploads/<upload_id>/source.ext
├── raw/...
├── corpus/...
├── data/index.db
└── reports/...
```

上传完成后：

1. 生成不可猜测的 `upload_id`；
2. 以二进制流写入临时文件，限制单文件大小和单批文件数量；
3. 写入完成后重新计算 SHA-256，大小和摘要作为审计元数据；
4. 原子重命名为 staging 完成态，未完成文件不可提交管线；
5. 通过后缀白名单只允许 `.md`、`.markdown`、`.pdf`；MIME 仅作辅助信号，不能单独信任；
6. 文件名只作为展示和元数据，业务路径使用服务生成的文件名；禁止把用户文件名直接拼进路径；
7. 调用 `PrismAPI.add_material`，由现有 IngestionService 负责 raw 留存、Markdown 标准化和索引；
8. `add_material` 成功建立材料正本后删除 staging 临时文件；失败时按保留策略清理并保留安全错误审计；
9. 服务异常退出后，启动清理过期 staging，不删除尚未确认完成的 corpus/raw 文件。

上传不接受任意本机路径作为浏览器输入。已有“项目本地路径追加”可以保留为 CLI/兼容入口，但不应作为新的 WebUI 主流程。

### 3.3 安全校验

在任何写操作前按顺序执行：

- `case_id` 来自 facade 的案例列表选择，不接受前端自由文本作为权威案例；
- 校验扩展名、大小、空文件和上传数量；
- 拒绝符号链接、目录、路径穿越、绝对路径和 staging 目录外的目标；
- 使用服务生成的 `upload_id` 和随机文件名，文件名按 Unicode 规范化仅用于显示，不能用于目录定位；
- PDF 的真实解析由既有 pdfplumber/OCR seam 完成；不因为伪造 MIME 就绕过解析错误；
- Markdown frontmatter、时间字段和内容 hash 继续由 IngestionService 校验；
- 不在 UI、状态接口、异常 toast 或报告中回显 API key、Cookie、完整 prompt、原始异常链或未脱敏绝对路径；
- 不把上传内容写入 URL、日志或 WebSocket 事件名称；
- 仅在 PRISM 的 loopback WebUI 上提供下载链接，下载必须按服务生成的 `version_id`/`export_id` 授权解析，不能接受任意文件路径。

## 4. 材料旅程与状态模型

### 4.1 用户可见的旅程

用户视图可以比底层 `PipelineRun` 更细，但每个细粒度状态必须有明确来源，不得伪造后台进度：

```text
selected
  → uploading
  → staged
  → ingested
  → indexed
  → extracted
  → merged
  → graph_written
  → analyzed
  → done(success | partial | failure)
```

建议投影关系：

| UI 状态 | 来源/判定 | 说明 |
|---|---|---|
| `selected` | 浏览器本地状态 | 尚未发送服务端 |
| `uploading` | NiceGUI 上传事件 | 字节仍在传输 |
| `staged` | staging 原子写入成功 | 尚未进入业务摄入管线 |
| `ingested` | `IngestionResult` 返回 | raw/corpus 材料已建立 |
| `indexed` | `PipelineStage(name="index")` 完成 | SQLite 索引完成 |
| `extracted` | `extract` 阶段完成且结果被验证 | 不等于语义质量通过 |
| `merged` | case ledger / case outcome 完成 | 材料已纳入目标案例 |
| `graph_written` | graph 阶段完成 | 离线后端与真实 Graphiti 必须分开标注 |
| `analyzed` | 产生分析或报告版本 | 报告生成失败时不能点亮 |
| `done` | 终态投影 | 伴随 success/partial/failure 质量状态 |

只要底层没有证据证明某阶段完成，就显示 `pending/unknown`，而不是按调用顺序补齐。`processing` 是当前进程的瞬态；持久化终态至少包括 `failed` 和 `committed`，对阶段历史的要求见第 2.2 节。

### 4.2 运行详情 DTO

建议 facade 返回如下 JSON-safe view model；真实 Python 实现可以使用 frozen dataclass：

```json
{
  "material": {
    "material_id": "mat-...",
    "display_name": "policy.pdf",
    "source_format": "pdf",
    "raw_saved": true,
    "corpus_path": "corpus/2026-09/source/title.md",
    "content_hash": "sha256:...",
    "case_id": "case-..."
  },
  "run": {
    "status": "completed",
    "correlation_id": "...",
    "started_at": "2026-09-06T02:00:00+00:00",
    "finished_at": "2026-09-06T02:01:00+00:00",
    "stages": [
      {"name": "index", "status": "completed", "detail": null},
      {"name": "extract", "status": "completed", "detail": null},
      {"name": "graph", "status": "skipped", "detail": "no case bundle"}
    ]
  },
  "outcome": {
    "status": "committed",
    "stage": null,
    "error_type": null,
    "message": null,
    "occurred_at": "2026-09-06T02:01:00+00:00"
  },
  "quality": {
    "mechanism_status": "pass",
    "semantic_status": "partial",
    "evidence_gap_count": 2,
    "evidence_gaps": ["..."],
    "graph_backend": "offline"
  },
  "report_links": [
    {"version_id": "report-...", "trigger": "material_added"}
  ]
}
```

`message` 必须使用现有审计脱敏逻辑输出短文本。对用户可显示的失败信息至少包含“阶段 + 错误类型 + 下一步建议”；原始异常只留在受控终端日志或内部审计中。

## 5. 状态刷新与并发

### 5.1 第一阶段采用轮询，事件作为内部触发

当前 EventBus 适合在服务内连接 `material.ingested` 到 `PipelineService`，不是跨进程持久化消息队列。第一版建议：

1. 上传回调提交材料并得到 `material_id`；
2. 页面保存当前 `material_id`，每 500ms–2s 轮询一次 `material_journey()`；
3. 读取到终态后停止轮询；
4. 页面刷新或重新打开时按 `material_id`/case 列表重新读取，不依赖浏览器内存；
5. 如后续需要 WebSocket 推送，可在 facade 外加事件桥，但事件仍必须以持久化查询结果为准。

不要把内存中的“正在执行”误写成已提交；不要因浏览器断开而取消后台管线，除非另有明确取消契约。

### 5.2 单进程并发与重复提交

- 每个上传批次具有 `upload_id`；同一上传重试携带同一 hash，服务端按 material/content hash 幂等；
- `material_id`、`correlation_id` 和报告 `input_hash` 继续沿用现有幂等规则；
- 同一材料只能有一个当前处理任务，重复点击显示“已在处理”并绑定到原任务；
- 失败后提供显式“重试”，重试复用已提交材料，不重复写 corpus，不重复合并 case；
- 页面关闭、刷新或服务重启后，不能创建第二个隐式任务；
- 报告相同输入必须由 `ReportVersionLedger.find_by_input_hash()` 去重，不能在 UI 层自行判断。

## 6. 报告中心

### 6.1 路由

建议新增：

```text
/reports
/reports/{version_id}
```

NiceGUI 若不适合使用动态路径，可使用 `/reports?version_id=...`，但 `version_id` 必须经过 facade `report_version()` 校验。

### 6.2 报告列表

`ReportController.list(case_id, as_of)` 只调用 `PrismAPI.report_versions()`，投影：

- `version_id`、`case_id`、`as_of`、`created_at`；
- `trigger`、`summary_origin`、`parent_version_id`；
- `markdown_hash`、`input_hash` 的截短展示，并提供复制完整值而不写入 URL；
- 关联的质量状态、evidence gap 数量和 PDF 是否已生成（若这些字段可由 facade 提供）。

列表按 `created_at` 降序展示，保留版本链和 cutoff。空列表与 facade 失败必须区分：失败显示错误状态，不能渲染成“没有报告”。

### 6.3 报告详情与 Markdown 阅读

详情页调用 `PrismAPI.report_version(version_id)`，读取 `ReportVersion.markdown` 并以只读 Markdown 渲染。建议同时提供：

- “原始 Markdown”只读代码区，便于核对标题、引用和哈希；
- 渲染视图，禁止执行任意 HTML/脚本；
- 版本元数据、输入 hash、Markdown hash、case、cutoff、trigger、父版本；
- `mechanism_status`、`semantic_status`、evidence gaps 的明确徽章；若版本模型暂时没有这些字段，显示 `unknown`，不能猜测；
- 报告生成失败、partial 或 no conclusion 的醒目说明。

报告正文可能来自 LLM，但页面只展示账本中已保存的正文。不要在前端二次调用 LLM 或根据正文重算事实。

### 6.4 证据回溯

报告中的 citation 应以结构化引用优先：`episode_key`、`source_id`、必要时的段落/页码/quote。详情页把引用渲染为安全锚点，点击后调用 facade 的证据查询/时间线详情接口；禁止前端按 corpus 路径自行打开任意文件。

如果当前 `ReportDocument` 只有 Markdown 文本而没有结构化 citation，第一阶段可以：

1. 显示正文中的纯文本引用；
2. 提供“按 source_id 搜索证据”的受限入口；
3. 将结构化引用持久化作为后续兼容增列，不能把正则猜到的文本引用当作已验证证据。

最终验收要求：能从至少一个真实报告结论跳到 `source_id`、corpus 路径、段落/页码和 quote，缺任一项就显示证据不完整。

### 6.5 PDF 导出与下载

导出按钮执行：

```text
version_id
  → PrismAPI.export_report_pdf(version_id, server_generated_output_path)
  → ReportPdfExportResult
  → download by opaque export_id
```

服务器输出路径必须位于 `PRISM_HOME` 下项目专用 reports/exports 目录，文件名由服务生成；前端不能传入任意路径。已有 PDF 与同一 Markdown hash 相符时可复用；内容冲突必须拒绝覆盖。导出完成后显示页数、版本 id、Markdown hash 和下载按钮；导出失败只显示安全错误类型及安装指引，不能暴露临时目录、密钥或异常全文。

## 7. Controller 与页面组织

建议新增以下无 NiceGUI 依赖的 controller：

```text
src/prism/webui/upload.py       # 上传事件/临时文件校验
src/prism/webui/journey.py      # 材料旅程查询与 JSON-safe projection
src/prism/webui/reports.py      # 报告列表、详情、PDF export projection
```

页面只负责控件、刷新和导航：

```text
src/prism/webui/app.py
  build_material_entry_page(...)
  build_report_pages(...)

src/prism/webui/server.py
  _LazyAPI.add_material(...)
  _LazyAPI.material_journey(...)
  _LazyAPI.material_journeys(...)
  _LazyAPI.report_versions(...)
  _LazyAPI.report_version(...)
  _LazyAPI.export_report_pdf(...)
```

`_LazyAPI` 仍然只代理 runtime 的 `api`。测试使用 fake facade 注入 controller；不启动 NiceGUI、不依赖真实 LLM 或 Graphiti。旧的 `MaterialEntryController.submit(path, ...)` 保留兼容测试和 CLI 路径，但新页面优先传入 staging 完成结果。

## 8. 错误、安全与可观测性

### 8.1 错误分类

至少区分：

```text
upload_rejected       # 后缀、大小、数量、空文件
staging_failed        # 临时文件写入/校验
ingestion_failed      # raw/corpus 标准化
index_failed          # SQLite index
extraction_failed     # JSON/schema/evidence/time/source 校验
case_merge_failed     # 案例绑定/冲突
graph_failed          # graph backend write
analysis_failed       # analyzer/report
pdf_export_failed     # renderer/pypdf/路径冲突
```

每类错误都应映射为稳定的用户短语和建议。底层 `PipelineFailure.stage/error_type/message` 是审计来源；UI 不能吞掉异常，也不能把异常状态改写为“完成”。

### 8.2 质量状态

统一使用 `loading/ready/success/partial/failure/unknown` 作为界面状态，但必须单独显示：

```text
pipeline_status
mechanism_status
semantic_status
evidence_gap_count
```

`pipeline=success` 只能表示管线运行完成；不代表语义结论正确。`mechanism=pass, semantic=partial` 应显示为“已完成机制处理，但语义质量仍有缺口”。

## 9. 分阶段实施

### Phase A：真实上传与单材料旅程（P0）

1. 新增 staging service 和上传 controller；
2. 扩展 facade 只读 journey/outcome 查询；
3. 将阶段审计持久化或补齐重启后可读的阶段投影；
4. 升级 `/materials`，支持选择案例、上传 MD/PDF、查看当前与历史材料；
5. 加入安全校验、幂等、失败和重试；
6. 用 fake facade 做 controller 测试，再做本机 HTTP smoke。

### Phase B：报告中心（P0）

1. 新增 report controller 和 `/reports`、`/reports/{version_id}`；
2. 列出版本并显示元数据；
3. 只读渲染保存的 Markdown；
4. 接入结构化 citation 或安全的 source_id 证据搜索；
5. 接入服务端 PDF 导出和 opaque download；
6. 验证版本不可变、路径安全和同输入幂等。

### Phase C：工作台联动（P1）

1. 案例主页显示材料数、最近运行、最新报告；
2. 旅程详情跳转时间线节点、证据和报告；
3. 加入跨会话最近运行列表、失败筛选和 audit export；
4. 事件桥或更细的实时进度（若持久化契约已经稳定）。

## 10. 测试与验收矩阵

### 单元与契约测试

- 上传：后缀、MIME 伪造、空文件、超大文件、数量上限、路径穿越、符号链接、Unicode 文件名；
- staging：部分写入不可提交、原子重命名、hash、过期清理、异常清理；
- facade：目标案例缺失/漂移被拒绝；只读 journey 不直接暴露底层依赖；
- journey：每个 `PipelineRun` 阶段正确映射；缺失阶段显示 unknown；失败包含 stage/error_type/message；
- 重试：同 hash/material_id 不重复 corpus、case merge、graph 或报告版本；失败转成功后清除过期失败状态；
- reports：列表、未知 version_id、正文 hash、父版本、cutoff、空列表/读取失败区别；
- evidence：有效 locator 可回溯，坏 locator 不生成可点击成功状态；
- PDF：服务端路径限制、同内容幂等、冲突拒绝、生成结果可回读；
- 安全：API key、prompt、正文和绝对路径不出现在错误/日志/URL。

### 集成与真实本机验收

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q
ruff check .
python -m compileall -q src tools
python -m prism.webui --host 127.0.0.1 --port 8765
```

验收脚本应使用临时 `PRISM_HOME` 和合成材料，逐项确认：

1. 浏览器选择 MD/PDF 后得到 `upload_id`；
2. `/materials` 显示材料已入库及 raw/corpus 元数据；
3. 旅程显示每个真实完成/跳过/失败阶段；
4. 失败可解释且重试不重复写入；
5. `mechanism`、`semantic`、evidence gap 分开显示；
6. `/reports` 列出保存的报告版本；
7. 详情页显示保存的 Markdown 正文与版本 hash；
8. citation 至少有一条可回到 source/locator；
9. PDF 生成并通过 `pypdf` 回读；
10. 服务仍只监听 `127.0.0.1`，四个旧路由和新报告路由均返回 200。

真实 provider、Graphiti、PDF renderer 的验收必须分别标记 `pass/partial/fail`。离线后端成功不能冒充真实 Graphiti；语义 partial 不能冒充语义 pass。

## 11. 迁移与兼容性

- 现有 `PRISM_HOME`、corpus、raw、index.db 和 `report_versions` 必须可直接打开；新增表采用 `CREATE TABLE IF NOT EXISTS` 或显式 schema migration；
- 旧的 `/materials` 路径提交方式保留为兼容路径，但文档和 UI 主入口改为浏览器上传；
- 旧报告无需重生成即可在 `/reports` 查看；缺少结构化 citation 的旧版本显示“引用结构不完整”，不能补猜；
- `ReportVersion` 和 PDF 导出接口保持向后兼容；若增加 citation/quality 字段，采用可选增列或单独关联表，不修改旧 Markdown hash 和 input hash；
- CLI 与 WebUI 继续共享 `PrismAPI`，任何 WebUI 专用 DTO 不得成为新的事实来源；
- 不迁移或读取 Hermes、Hindsight、私人 OpenViking、私人记忆或其他项目数据。

## 12. 完成定义

本迭代只有在以下全部成立时才可称为“WebUI 工作台可用”：

- 用户不需要先把文件放进服务器目录或手写本机路径，即可从浏览器上传 MD/PDF；
- 每份材料可在一次会话和重启后查看其真实旅程、终态、失败原因和质量状态；
- 页面不会把 pending、partial、unknown 或 failure 渲染成 success；
- 用户能从 WebUI 打开报告列表和具体版本，阅读保存的 Markdown；
- 报告引用能安全回溯到证据，无法回溯时明确显示缺口；
- 用户能从报告详情导出并下载 PDF，且不允许任意路径读写；
- 旧 CLI/API/语料/报告兼容，离线测试、lint、编译和本机 HTTP 冒烟通过；
- 真实 provider、Graphiti 和语义质量的结果按实际验证分别报告，不夸大。
