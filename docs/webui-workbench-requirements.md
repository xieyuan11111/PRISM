# PRISM WebUI 工作台需求文档（Workbench Requirements）

> **版本**：v1.0（待评审）
> **日期**：2026-09-06
> **性质**：PRISM v1 之后的 WebUI 工作台迭代（对应 `docs/v1-scope.md` 第 4 节延后项的正式需求化）
> **关联文档**：`REQUIREMENTS.md`（v0.4 需求基线）、`PROJECT.md`、`docs/v1-scope.md`、`docs/webui-getting-started.md`、`docs/m3-webui-case-home.md`、`docs/m3-webui-evidence-material-entry.md`
> **读者**：产品（第 2、3、4、9、10 章）、开发（第 5、6、7、8、11 章）、验收（第 10、11 章）

---

## 1. 文档定位

本文档定义 PRISM WebUI 从"查询与一次性操作界面"升级为**单用户日常工作台**的需求：让用户在浏览器里完成"上传材料 → 看见流转 → 看见失败与质量 → 读到报告 → 回溯证据 → 导出 PDF"的完整闭环。

本编号体系为 `WB-x.y`（WorkBench），与 `REQUIREMENTS.md` 的 `FR-x.y` 互不冲突，映射关系见第 12 章。本文档只新增需求，不修改既有基线。

v1 已交付的页面（`/` 案例主页与历史快照、`/debate`、`/evidence`、`/materials`，见 `src/prism/webui/app.py`）保持既有行为，本迭代在其上升级与新增页面。

---

## 2. 背景与问题陈述

### 2.1 v1 现状

v1 是本机单用户、loopback-only 的初步产品（`docs/v1-scope.md`）。WebUI 四个页面共用 `PrismAPI` facade（CLI 与 WebUI 同一契约，FR-8.10），其中：

- `/materials`（`src/prism/webui/materials.py`）只接受**用户手动输入的项目本地路径**，校验后委托 `PrismAPI.add_material`，并在单次操作后显示一次 pipeline 阶段审计、报告版本元数据与辩论链接哈希；
- 报告能力（版本账本、PDF 导出）已通过 CLI（`report`、`report-versions`、`report-version`、`report-pdf`、`rebuild`）与 facade（`report_versions` / `report_version` / `export_report_pdf` / `rebuild_report`）交付，但 **WebUI 没有任何报告页面**；
- 材料生命周期结果账本（`src/prism/pipeline/outcomes.py`，`pipeline_outcomes` 表：`pending`/`failed`/`committed`，失败记录带 `stage`/`error_type`/`message`）已持久化在 SQLite，但 WebUI 无法查询它。

### 2.2 要解决的四个问题

| 编号 | 问题 | 现状证据 |
|---|---|---|
| P-1 | **浏览器不能真正上传材料**：用户无法在浏览器里选择/拖拽文件，只能先 manually 把文件放到本地再输入路径 | `materials.py` 的 `_validated_path` 只接受已存在的本地 MD/PDF 路径；`v1-scope.md` 第 4 节明确把拖拽上传延后 |
| P-2 | **材料流转不可见**：用户看不到一份材料从上传、raw 留存、Markdown 标准化、索引、抽取、案例合并、图谱写入到分析的全链路状态 | 阶段审计只在 `/materials` 单次追加后一次性渲染（`_status_markdown`）；没有材料列表视图、没有跨会话的旅程视图；持久化的 `pipeline_outcomes` 无查询入口 |
| P-3 | **失败原因与质量状态不可见**：失败只显示异常类名；证据缺口、mechanism/semantic 状态只在单次操作后一闪而过 | `status.py` 的 `safe_error_text` 只返回 `"{operation} failed ({异常类名})"`；`outcome_status` 的 mechanism/semantic/evidence-gap 投影仅用于 `/materials` 操作结果，材料与报告层面无持续展示 |
| P-4 | **报告不可见**：用户在 WebUI 看不到最终生成的报告——没有报告列表、没有 Markdown 阅读、没有证据回溯入口、没有 PDF 导出入口 | `app.py` 只注册 `/`、`/debate`、`/evidence`、`/materials`；报告阅读与导出仅存在于 CLI；`report_version_view` 有意省略 Markdown 正文 |

### 2.3 核心问题重述

> 用户把一份材料交给 PRISM 之后，能否在浏览器里回答：**它现在到哪一步了？卡在哪、为什么？质量是 pass、partial 还是 failure？最终长成了哪份报告？报告里的每个结论回到哪条证据？**

v1 的答案是"去终端看日志、去 CLI 查账本、去文件系统找 PDF"。工作台迭代的答案是：这些全部成为 WebUI 的一等能力。

---

## 3. 目标与既有边界

### 3.1 目标

1. 用户在浏览器中真实上传本地 MD/PDF 文件（文件选择与拖拽），文件经同一摄入管线进入 PRISM；
2. 每份材料的全链路旅程（上传 → raw 留存 → Markdown 标准化 → 索引 → 抽取 → 案例合并 → 图谱写入 → 分析产出）在 WebUI 可见、可回看、可审计；
3. 失败阶段、错误类型、脱敏错误消息、证据缺口、mechanism/semantic 分层状态对用户持续可见，且从不伪装成成功；
4. 报告成为 WebUI 一等对象：**报告列表、报告详情 Markdown 阅读、证据回溯、PDF 下载/导出入口**为 P0 需求。

### 3.2 硬边界（所有 WB 需求的前提，编号 H-1 至 H-6）

任何一条 WB 需求都不得违反以下既有边界；冲突时以边界为准：

| 编号 | 边界 | 含义 |
|---|---|---|
| H-1 | **loopback-only** | WebUI 仅绑定 `127.0.0.1`/`localhost`/`::1`（`server.py` 已拒绝非 loopback 绑定）；不做认证、不做远程暴露；远程访问是用户自己的反向代理决策 |
| H-2 | **单用户** | 不做多用户、角色与权限分级；但一切数据修改仍必须经 `PrismAPI` facade 完成（自动管线是唯一写路径）并留下审计 |
| H-3 | **目标案例显式声明** | 上传必须显式选择一个**已记录**的 case_id（来自案例列表），绝不按标题、标签或向量猜测；未知 case_id 直接拒绝 |
| H-4 | **语义诚实** | mechanism 与 semantic 分层展示；`semantic=partial` 不是崩溃但**绝不被包装成 success**；LLM 提炼失败不得伪造摘要；界面不得为了"绿色"改写质量判定 |
| H-5 | **不绕过访问控制** | 上传只接收用户自有本地文件；不做"替用户抓取登录墙/付费墙内容"的任何入口；材料获取继续遵守公开访问边界 |
| H-6 | **不创建平行事实库** | 一切状态与旅程视图只是审计数据/账本/快照的只读投影（`pipeline_outcomes`、`PipelineRun` 阶段审计、`report_versions`、`IndexEntry`、`HistoricalCaseState`）；如为跨重启保留阶段审计而增加版本化运行审计表，也只能是可重建的运行状态账本，不能承载新的事实、结论或时间语义；UI 不做时间过滤、不做事实推断 |

---

## 4. 用户流程

### 4.1 主流程：上传一份材料并看着它走完全程

```text
1. 用户启动 WebUI（python -m prism.webui，loopback），进入 /materials
2. 用户通过【选择文件】或【拖拽】提供一个本地 MD/PDF
   （P0 单文件；批量属 P1）
3. 用户从案例选择器中显式选择目标 case_id（选择器数据来自
   PrismAPI.case_overviews()，只能选、不能猜、不能手填未知 id）
4. 可选：填写 as_of（timezone-aware）、父辩论 run、是否使用 LLM 生成报告
   （默认关闭，与 v1 /materials 一致）
5. 点击「提交」：
   a. 文件字节经浏览器上传，落盘到 PRISM_HOME 受控 staging 目录
   b. 界面进入 processing 状态，按步骤推进展示旅程：
      上传 → raw 留存 + Markdown 标准化 → 索引 → 抽取 → 案例合并
      → 图谱写入 → 分析（报告版本保存）
   c. 每步显示：状态徽章、时间、可展开的审计信息
6. 完成后：
   - success：显示 material_id、case_id、报告版本 id，
     提供「查看报告」深链到 /reports
   - partial：同上，但 semantic 状态明确显示 partial，
     证据缺口列表可见
   - failure：显示失败步骤、error_type、脱敏 message，
     不显示任何成功假象
7. 用户随后可在材料列表中随时回看这份材料的旅程（跨会话可查）
```

### 4.2 失败路径

```text
前置校验失败（未选 case / 未知 case_id / 非 MD/PDF / 超大小 / 空文件）
  → 提交被拒绝，明确指出原因，不产生任何写入
管线中途失败（如抽取阶段 LLM 失败、案例绑定冲突）
  → 旅程停在第 N 步并标红；显示 stage、error_type、脱敏 message
  → 材料列表中标红；持久账本（pipeline_outcomes）记录该失败，
    重启 WebUI 后仍可见
  → P1：提供「重试」入口（语义等同再次调用 process_material：
    成功后清除陈旧失败记录，持久失败不伪造成功）
```

### 4.3 报告阅读与证据回溯流程

```text
1. 用户进入 /reports，看到全部案例的报告版本列表
   （case_id、as_of、created_at、trigger、parent_version_id、
    summary_origin、input_hash/markdown_hash）
2. 点击一个版本进入详情页：
   a. 渲染阅读该版本的 Markdown 正文（只读，版本不可变）
   b. 查看版本元数据与溯源链（parent version、trigger、hash）
   c. 【证据回溯】点击报告中的 source_id 引用 →
      定位到证据：source_id、corpus_path、段落/页码、quote
      （复用既有 evidence locator 投影，或深链 /evidence 检索）
3. 点击【导出 PDF】：
   - 已安装 pdf extra：导出为派生 PDF（浏览器下载或写入
     PRISM_HOME 受控 exports 目录，二选一由实现决定，验收只要求
     "入口存在且产物可得"）
   - 未安装 pdf extra：显示与 CLI 一致的明确安装指引错误
   - 导出失败（如无 Edge/Chromium）：显示明确错误，不产出半成品
```

---

## 5. 功能需求（P0/P1）

优先级含义：P0 = 工作台迭代必须交付；P1 = 应交付但可在后续小版本补齐。

### WB-1 浏览器材料上传（解决 P-1）

| 编号 | 需求 | 优先级 |
|---|---|---|
| WB-1.1 | `/materials` 提供浏览器原生文件选择与拖拽上传，接收 MD/PDF 文件字节 | P0 |
| WB-1.2 | 上传文件先落盘到 `PRISM_HOME` 受控 staging 目录（文件名规范化，不使用客户端原始名拼路径，防路径穿越），再以该路径走既有 `add_material` 管线——**绝不绕过 `IngestionService`**（raw 留存、Markdown 标准化、frontmatter 全部由既有管线完成） | P0 |
| WB-1.3 | 客户端原始文件名保留在 metadata 中作为审计信息，不影响 corpus 落盘命名 | P0 |
| WB-1.4 | 上传前校验：仅 `.md`/`.markdown`/`.pdf` 后缀、非空文件、大小不超过可配置上限（建议默认 50 MB）；拒绝时给出明确原因 | P0 |
| WB-1.5 | 目标案例只能从 `case_overviews()` 返回的已有案例中选择（H-3）；未选择或选择失效时拒绝提交 | P0 |
| WB-1.6 | `use_llm` 默认关闭；as_of（timezone-aware）与父辩论 run 沿用 v1 语义 | P0 |
| WB-1.7 | 多文件批量上传（逐文件独立校验、独立旅程，一次失败不中断其余） | P1 |
| WB-1.8 | 上传大文件时显示进度；OCR 属摄入内部步骤，实时 OCR 进度流不在本期范围（见非目标） | P1 |

> **设计说明**：staging 落盘复用既有 source 抓取的 spool 模式（`spool_source_item` → `source_raw_dir` 下的 spool 目录）：浏览器上传与 URL 抓取在"进入摄入管线之前先安全落盘"这一点上同构。上传目录是暂存区，不是新的长期存储；raw 正本仍由摄入管线统一留底到 `raw/`。

### WB-2 材料流转旅程视图（解决 P-2）

| 编号 | 需求 | 优先级 |
|---|---|---|
| WB-2.1 | 提供材料列表视图：每份材料显示 material_id、标题、案例、当前生命周期状态（committed / failed / processing）、最近时间；数据来自证据库与 `pipeline_outcomes` 账本的只读聚合 | P0 |
| WB-2.2 | 提供单材料旅程视图，按固定顺序展示七个步骤及各自状态与时间：**上传（staged）→ raw 留存 + Markdown 标准化（ingested）→ 索引（indexed）→ 抽取（extracted）→ 案例合并（merged）→ 图谱写入（graph_written）→ 分析/报告版本（analyzed）** | P0 |
| WB-2.3 | 每一步可展开审计信息：对应 pipeline stage 的 name/status/detail（既有 `PipelineStage` 审计）、案例合并结果、图谱写入结果、报告版本 id；旅程是**投影**，不新算任何事实（H-6） | P0 |
| WB-2.4 | 旅程视图同时展示 raw 留存路径与 corpus Markdown 路径（项目相对路径），确认"原文留底 + 标准化正本"都存在 | P0 |
| WB-2.5 | 失败材料在列表中标红并可筛出；失败详情见 WB-3 | P0 |
| WB-2.6 | 需要在 `PrismAPI` 增加只读查询入口（如 `pipeline_outcomes()` 与 `material_journey(material_id)`），CLI 与 WebUI 共用（维持 FR-8.10 的"同 facade"原则）；只读、幂等、不触发任何管线 | P0 |
| WB-2.7 | 旅程自动刷新（轮询或推送），无需手动刷新页面 | P1 |
| WB-2.8 | 失败材料「重试」入口：语义等同对该 material_id 再次调用 `process_material`（既有幂等重试语义），成功后清除陈旧失败状态 | P1 |
| WB-2.9 | corpus Markdown / raw 原文只读预览（受第 8 章展示约束；v1 文档已将原文浏览列为后续项） | P1 |

> **状态映射说明**（开发约束）：七个旅程步骤映射到既有审计数据——staged（staging 落盘，WebUI 层事实）、ingested（`IngestionResult.raw_path`/`corpus_path` 已生成）、indexed/extracted/graph_written（`PipelineRun.stages` 的 `index`/`extract`/`graph` 三阶段，`extract` 含案例累积合并、其结果经 `case_outcome` 观察）、analyzed（`ProcessMaterialResult.report_version` 非空）。**UI 不发明新状态机，只把上述字段投影为步骤徽章**；`pending` 是进程内瞬态（账本只持久化 `failed`/`committed`），对应界面的 processing 显示。

### WB-3 失败原因与质量状态透明（解决 P-3）

| 编号 | 需求 | 优先级 |
|---|---|---|
| WB-3.1 | 旅程失败步骤展示三要素：**失败阶段（stage）、错误类型（error_type）、脱敏错误消息（message）**；数据来自 `pipeline_outcomes` 与阶段审计 | P0 |
| WB-3.2 | 错误消息展示遵守脱敏白名单：只展示受控错误类型（如 `PipelineError`、`MaterialCaseConflict`、`SourceFetchError` 分类）的 message；默认仍为 `safe_error_text` 级别的"操作名 + 异常类"；**任何情况下不展示 API key、prompt、材料正文、secrets** | P0 |
| WB-3.3 | 机制/语义分层状态（`outcome_status` 投影：`mechanism_status`、`semantic_status`、`evidence_gap_count`）在旅程视图与材料列表中持续可见，而非仅单次操作后可见 | P0 |
| WB-3.4 | `semantic=partial` 显示为 partial（警示色），不显示 success；`unknown` 显示为 unknown，不得默认渲染成通过（H-4） | P0 |
| WB-3.5 | 证据缺口（evidence gaps）与未解决冲突（unresolved conflicts）作为旅程/案例视图中的独立列表可见（gap_type / item_kind / detail） | P0 |
| WB-3.6 | 任何失败路径上界面不出现 success 字样或成功色（"从不伪造成功"的可视化不变量） | P0 |
| WB-3.7 | 错误消息按错误类型提供"下一步建议"文案（如 LLM 未配置 → 指向安装文档；案例冲突 → 说明材料已绑定其他案例） | P1 |

### WB-4 报告中心（解决 P-4；一等需求）

| 编号 | 需求 | 优先级 |
|---|---|---|
| WB-4.1 | **报告列表页 `/reports`**：列出全部报告版本（可按 case 过滤），列包含 version_id、case_id、as_of、created_at、trigger（initial/material_added/rebuild/debate_updated）、parent_version_id、summary_origin、input_hash/markdown_hash；数据来自 `PrismAPI.report_versions()`，按创建顺序排列 | P0 |
| WB-4.2 | **报告详情页**：渲染阅读该版本的 Markdown 正文（`ReportVersion.markdown`，只读），同时展示版本元数据与溯源链；版本不可变——详情页没有任何编辑入口 | P0 |
| WB-4.3 | **证据回溯**：报告中的 source_id 引用可点击，定位到证据——source_id、corpus_path、段落/页码、quote（复用案例快照的 evidence locator 投影，或深链 `/evidence` 检索该 source_id）；从"报告结论"到"原文位置"的路径必须可走通 | P0 |
| WB-4.4 | **PDF 导出入口**：详情页提供「导出 PDF」，委托 `PrismAPI.export_report_pdf(version_id, ...)`；PDF 是派生产物，导出不改变版本；`pdf` extra 缺失或渲染失败时显示与 CLI 一致的明确错误 | P0 |
| WB-4.5 | 报告的 mechanism/semantic 质量状态在详情页可见（沿用 H-4：LLM 摘要 semantic-partial 时详情页明确标注 partial，`summary_origin` 如实展示） | P0 |
| WB-4.6 | 案例主页（`/`）与旅程视图的 `analyzed` 步骤提供到 `/reports` 的深链 | P0 |
| WB-4.7 | 版本对比视图：基于 parent_version_id 的两个版本差异阅读（新增/变化的关键结论） | P1 |
| WB-4.8 | 「重建报告」入口：委托 `rebuild_report`（trigger=rebuild），生成新版本而非覆盖 | P1 |
| WB-4.9 | 导出产物统一落入 `PRISM_HOME` 受控 exports 目录（或浏览器下载，实现二选一），路径对用户可见、可配置 | P1 |

> **实现注记**：facade 层报告 API（`report_versions`、`report_version`、`export_report_pdf`、`rebuild_report`、`save_report_version`）与版本账本（`src/prism/report/ledger.py`，`report_versions` 表已存 Markdown 正文与全部元数据）**已完整存在**，本需求主要是新增 WebUI 页面 + controller（沿用依赖注入 seam 模式）+ `server.py` 的 `_LazyAPI` 暴露对应方法。报告文档的结构化 citations（episode_key/source_id 对）可用于 WB-4.3 的锚点实现；如需在版本账本持久化结构化引用，只允许增列，不得改变版本不可变语义。

### WB-5 横切需求

| 编号 | 需求 | 优先级 |
|---|---|---|
| WB-5.1 | 所有新视图沿用既有状态徽章语义：loading / ready / success / partial / failure / unknown（`docs/webui-getting-started.md` 第 9 节），全站色彩与文案一致 | P0 |
| WB-5.2 | 新 controller 均为依赖注入、无 NiceGUI 依赖的可测 seam（沿用 `MaterialEntryController` 模式），离线测试不启动浏览器 | P0 |
| WB-5.3 | WebUI 与 CLI 输出一致：旅程/报告视图只投影 facade 返回值，不另做过滤、排序外的任何数据加工（排序/分页属展示层） | P0 |
| WB-5.4 | 页面加载失败（如 facade 未就绪、账本不可用）显示明确错误，不渲染空列表冒充"没有数据" | P0 |

---

## 6. 页面与交互

### 6.1 页面清单

| 路由 | 状态 | 内容 |
|---|---|---|
| `/materials` | 升级 | 上传区（选择/拖拽 + 校验反馈）、目标案例选择器（只选不猜）、可选参数（as_of / 父辩论 run / use_llm）、材料列表（含失败筛选）、单材料旅程详情 |
| `/reports` | 新增 | 报告版本列表（按 case 过滤）→ 详情（Markdown 阅读 + 元数据 + 证据回溯 + PDF 导出） |
| `/` | 增强 | 案例行提供最新报告版本入口（深链 `/reports`）；快照证据面板维持既有 locator 展示 |
| `/evidence` | 增强 | 检索行提供「查看旅程」深链（P1） |
| `/debate` | 维持 | 本迭代不改动（辩论 stale 提示可深链 `/materials`，P1） |

### 6.2 `/materials` 交互要点

- 上传区是页面主操作：拖拽高亮、后缀/大小即时校验、拒绝原因内联显示；
- 案例选择器：下拉来自 `case_overviews()`（显示 case_id + canonical_name），**无自由文本输入 case_id**（防止"猜案例"绕过 H-3；高级用户可通过既有 CLI 处理未记录案例）；
- 提交后页面**原地推进**旅程步骤（processing → 各步骤徽章逐个点亮/标红），完成后 `analyzed` 步骤内嵌「查看报告」按钮；
- 材料列表支持按状态（committed/failed/processing）与案例过滤；行点击展开旅程详情（七步骤 + 审计信息 + raw/corpus 路径）。

### 6.3 `/reports` 交互要点

- 列表默认按 created_at 降序，可按 case 过滤、按 trigger 过滤；
- 详情页分三区：元数据区（版本溯源链）、正文区（渲染 Markdown，只读）、操作区（证据回溯开关、导出 PDF）；
- 证据回溯：正文中的 source_id 引用高亮可点击，侧栏显示对应 locator（source_id、corpus_path、段落/页码、quote）或跳转 `/evidence?query=<source_id>`；
- 导出进行中显示 loading；成功显示产物位置（或触发下载）；失败显示明确错误与安装指引。

---

## 7. 状态机

### 7.1 上传会话（UI 层，瞬态）

```text
idle → selected（已选/已拖入，本地校验通过或给出拒绝原因）
     → uploading（字节传输中）
     → staged（已落盘 staging，尚未进入管线）
     → processing（add_material 执行中；旅程步骤随之推进）
     → done(success | partial | failure)
```

`partial` = 机制完成（mechanism=pass）但 semantic=partial 或存在证据缺口——它是终态之一，不是 failure，也不是 success（H-4）。

### 7.2 材料旅程（持久视图，投影既有审计数据）

```text
staged ──→ ingested ──→ indexed ──→ extracted ──→ merged ──→ graph_written ──→ analyzed
(raw+MD)    (index)      (extract)    (案例累积)    (graph)      (report_version)

任意步骤可进入：failed(stage, error_type, message)  ← 持久化于 pipeline_outcomes
processing 为进程内瞬态（对应账本 pending，不持久化）
committed = 账本视角的"运行完成"（对应已走到 merged 及之后）
```

规则：

- 旅程状态是**只读投影**：由 staging 记录、`IngestionResult`、`PipelineRun.stages`、`case_outcome`、`report_version` 推导，不创建新的事实库；若为跨重启保留阶段审计而增加版本化运行审计表，只能保存可重建的运行状态，不能引入新的事实或时间语义（H-6）；
- 失败可发生在任意步骤；失败后旅程停在该步骤，重试成功后整条旅程重新点亮并覆盖陈旧失败（与 `process_material` 幂等重试语义一致）；
- 跳过的阶段（如非 fulltext 材料 extract/graph 被跳过并留审计）显示 skipped 及原因，不显示为失败。

### 7.3 报告版本（不可变）与导出

```text
版本：initial | material_added | rebuild | debate_updated 触发产生 → 永不修改
     （同 input_hash 幂等返回既有版本；parent_version_id 构成溯源链）

导出：idle → exporting → exported(产物路径/下载) | failed(明确错误)
     （PDF 是派生工件，导出零副作用，不回写版本）
```

---

## 8. 权限与安全

1. **绑定边界（H-1）**：继续仅允许 loopback 绑定；非 loopback host 在导入 NiceGUI 之前即被拒绝（维持 `server.py` 现行为）。本迭代不引入认证——正因如此，一切能力都必须留在本机。
2. **单用户写边界（H-2）**：上传、重试、重建报告等数据修改操作全部经 `PrismAPI` facade 走自动管线；WebUI 自身不写 corpus、不写图谱、不直接操作 SQLite 业务表（staging 落盘与 exports 输出是受控文件区，不是事实存储）。
3. **上传安全**：
   - staging 目录位于 `PRISM_HOME` 内，目录名固定、不可由请求参数指定；
   - 落盘文件名服务端生成（规范化/随机化），客户端文件名只进 metadata；
   - 只接收 MD/PDF 后缀与非空文件，大小上限可配置；
   - 上传内容一律当作**数据**处理：不执行、不渲染到管理面、不解析为模板。
4. **展示脱敏**：
   - 错误展示遵循 WB-3.2 白名单；不展示 API key、prompt、材料正文与系统绝对路径（corpus_path 等项目相对路径可展示，与 v1 locator 展示一致）；
   - 报告正文与证据 quote 属产品输出，单用户本机阅读是预期功能，不受"错误消息脱敏"约束，但不得进入日志。
5. **不绕过访问控制（H-5）**：上传的文件必须是用户自有内容；工作台不提供任何"输入 URL 替用户抓取"的新入口（既有 source fetching 的公开访问边界不变）。
6. **审计**：上传材料天然进入既有事件总线与账本审计（`material.ingested` 事件、`pipeline_outcomes`、`report_versions`）；staging 文件在摄入完成后可清理，raw 正本以 `raw/` 为准。
7. **密钥**：API key 仍只在本地受保护 secrets/环境变量；新页面不出现明文密钥展示，`use_llm` 开关不改变密钥边界。

---

## 9. 非目标

- ❌ 多用户认证、角色权限、远程/局域网暴露（H-1/H-2 的反面，明确不做）；
- ❌ WebUI 爬虫/研究任务调度入口（材料获取仍走既有 CLI/API 边界）；
- ❌ 实时流式辩论、暂停/继续/重启辩论控制（另案）；
- ❌ 模型设置页（FR-8.8，另案）；
- ❌ OCR 进度实时流（OCR 是摄入内部步骤，完成状态可见即可）；
- ❌ 把 semantic-partial 美化为成功的任何展示逻辑（H-4）；
- ❌ UI 侧时间过滤或事实推断（FR-8.10/H-6：时间语义只属于 analyzer/graph 层）；
- ❌ 报告在线编辑、协作或评论（版本不可变）；
- ❌ 为旅程/状态视图新建独立数据库或事实表（只读投影既有账本）；
- ❌ 移动端适配。

---

## 10. 验收标准

以下为 P0 验收清单（验收环境：本机、`PRISM_HOME` 独立数据目录、按 `webui-getting-started.md` 安装 `webui` extra；LLM 与 PDF 相关项分别需要 `openai-sdk`/`pdf` extra）。

### A. 上传

- [ ] A1 浏览器文件选择与拖拽均可上传 MD/PDF 到显式选择的目标案例；上传后材料出现在证据检索与案例材料列表中；
- [ ] A2 非 MD/PDF、空文件、超限文件被拒绝且原因明确；staging 落盘文件名与客户端文件名无关；
- [ ] A3 上传材料的 raw 正本与 corpus Markdown 均生成，旅程视图显示两个项目相对路径；
- [ ] A4 未选择案例或案例不存在时提交被拒绝，错误信息可理解，无任何写入发生。

### B. 旅程

- [ ] B1 单材料旅程按第 7.2 节七步骤顺序展示，每步有状态徽章与时间；
- [ ] B2 每步可展开审计信息（stage name/status/detail、案例合并与图谱写入结果、报告版本 id），不出现密钥/prompt/正文/绝对路径；
- [ ] B3 被跳过的阶段显示 skipped 及原因，不显示为失败；
- [ ] B4 材料列表跨会话可查：重启 WebUI 后，已 committed 与 failed 材料的状态仍正确（来自持久账本）。

### C. 失败与质量

- [ ] C1 人为制造失败（如断开 LLM 配置、绑定冲突）后，失败步骤、error_type、脱敏 message 可见，与账本记录一致；
- [ ] C2 semantic=partial 的材料/报告显示 partial（非 success 色）；evidence gaps 与 unresolved conflicts 列表可见；
- [ ] C3 全部失败路径走查：界面任何位置不出现 success 假象（含上传前置校验失败、管线失败、报告导出失败）。

### D. 报告

- [ ] D1 `/reports` 列表展示全部版本及第 5 章 WB-4.1 所列字段，按 case 过滤可用；
- [ ] D2 详情页可渲染阅读版本 Markdown，元数据与溯源链（parent version、trigger、hash）完整，且无任何编辑入口；
- [ ] D3 证据回溯可走通：从报告中任一 source_id 引用到达 locator（corpus_path + 段落/页码 + quote）或 `/evidence` 检索结果；
- [ ] D4 PDF 导出入口存在：成功导出产物可得（下载或 exports 目录）；缺失 `pdf` extra 时显示与 CLI 一致的明确指引；导出后版本内容与 hash 不变；
- [ ] D5 报告详情页如实展示 summary_origin 与 semantic 状态（LLM 摘要 partial 时明确标注）。

### E. 边界回归

- [ ] E1 非 loopback host 绑定仍被拒绝（既有 server 行为不回退）；
- [ ] E2 staging 与 exports 目录均位于 `PRISM_HOME` 内且不出现在 Git 工作区；
- [ ] E3 既有离线测试套件全绿；新增 controller 测试不依赖 NiceGUI/浏览器/网络；
- [ ] E4 旅程/报告视图为纯投影：实现中无任何新增事实表、无 UI 侧时间过滤（代码审查项）；
- [ ] E5 CLI 与 WebUI 对同一数据（如同一版本、同一材料 outcome）展示一致。

---

## 11. 与当前 v1 的差距

| # | 能力 | v1 现状 | 工作台目标（P0） | 主要改动点 |
|---|---|---|---|---|
| 1 | 材料进入方式 | `/materials` 仅路径文本输入（`materials.py: _validated_path`） | 浏览器文件选择 + 拖拽上传 → staging → 同一管线 | 上传组件、staging 落盘（复用 spool 模式）、controller 扩展 |
| 2 | 材料流转可见性 | 单次追加后一次性阶段审计（`_status_markdown`） | 七步骤旅程 + 材料列表，跨会话持久可查 | 新增旅程/列表视图；`PrismAPI` 增加只读 `pipeline_outcomes()`/`material_journey()` 入口；`server._LazyAPI` 暴露 |
| 3 | 失败原因 | `safe_error_text` 仅"操作名 + 异常类名" | stage + error_type + 白名单脱敏 message | 展示规则扩展（仍不泄密） |
| 4 | mechanism/semantic/证据缺口 | 仅 `/materials` 单次操作结果中一次性显示 | 旅程、材料列表、报告详情持续显示 | 复用 `outcome_status` 投影接入新视图 |
| 5 | 报告列表 | 无（CLI `report-versions`） | `/reports` 列表 + 过滤 | 新页面 + controller（facade 已有 `report_versions()`） |
| 6 | 报告阅读 | 无（CLI `report-version` 输出 JSON；账本已存 Markdown） | 详情页渲染阅读 Markdown + 元数据 | 新页面（`report_version_view` 需增加正文投影） |
| 7 | 证据回溯 | 案例快照 locator 已可点击（`app.py: _detail_markdown`）；报告侧无 | 报告 source_id 引用 → locator / `/evidence` 深链 | 报告详情锚点 + locator 面板 |
| 8 | PDF 导出 | 仅 CLI `report-pdf <id> <path>` | 详情页导出入口（下载或 exports 目录） | 页面按钮 + `export_report_pdf` 委托 + 缺依赖错误处理 |
| 9 | 写边界 | facade 唯一写路径，loopback 强制 | 不变 | 无（回归项 E1/E4 保障） |

差距 5–8 共享同一结论：**facade 与账本层能力已齐备，缺口集中在 WebUI 表现层与少量只读查询入口**，因此本迭代以 UI/controller 工作为主，不动数据模型与管线语义。

---

## 12. 与 REQUIREMENTS.md 的映射

| WB 需求组 | 对应基线需求 |
|---|---|
| WB-1 上传 | FR-1.1/1.8/1.9/1.13（多格式摄入、raw 留存）、FR-8.7（摄入入口 v0 的后续） |
| WB-2 旅程 | FR-8.9（数据修改留审计）、FR-8.10（同 facade 投影） |
| WB-3 失败与质量 | FR-1.6（失败可重试、记录原因）、NFR-5（失败返回明确错误，不伪造结论）、v1-scope 第 6 节（semantic-partial 不包装成 pass） |
| WB-4 报告中心 | FR-6.1/6.2/6.3（报告、结论回溯源）、FR-6.5（摘要可溯源）、FR-6.6（MD 转 PDF） |
| 硬边界 H-1..H-6 | REQUIREMENTS §13.7（安全边界）、§3.2/FR-3 设计说明（显式目标案例）、NFR-2/3/6（可追溯、隔离、合规）、M3 验收记录（不建平行事实库） |

---

*本文档为 PRISM WebUI 工作台迭代需求基线 v1.0，评审通过后作为 v1.x 工作台迭代的验收依据。*
