# PRISM CLI 优先 + AI 可调用 — 需求文档

> **版本**：v1.0（待评审）
> **日期**：2026-09-14
> **性质**：产品方向调整——WebUI 停止新增投入，CLI 成为一等交互界面，并明确支持 AI/agent 程序化调用
> **关联文档**：`REQUIREMENTS.md`（能力基线）、`docs/webui-workbench-requirements.md`（WebUI 工作台，仅存量保留）
> **读者**：产品（第 2、3、4、9 章）、开发（第 5、6、7、8 章）、验收（第 10 章）

---

## 1. 文档定位

本文档把 PRISM 的交互界面方向收敛为：

> **CLI 为唯一持续投入的一等界面；所有数据与操作能力必须可被 AI agent 以非交互、机器可读、可编排的方式调用。**

WebUI 已交付的能力（上传、旅程、报告阅读、证据回溯）**存量保留、不再新增功能**；后续新增能力一律先在 CLI 落地。

需求编号体系为 `C-x.y`（CLI-first）。

---

## 2. 背景与问题陈述

### 2.1 现状

PRISM 的 CLI 已经具备良好的机器可读基础：

- 21 个子命令：`search / cases / timeline / state / snapshot / compare / ingest / process / merge-case / bind-material / report / report-versions / report-version / report-pdf / add-material / rebuild-report / debate / follow-up / adjudication-history / fetch / fetch-all / discover / research`；
- 成功输出为 **stdout 单行 JSON**（`_write_json`），错误输出为 **stderr JSON**（`{"error": {message, type}}`）且退出码非 0；
- 敏感字段自动 `[REDACTED]`；
- 全程非交互，不读取 stdin；
- `main()` 支持注入 facade，可测试、可嵌入。

### 2.2 差距（CLI 要解决的问题）

| 编号 | 问题 | 现状证据 |
|---|---|---|
| G-1 | **没有 `prism` 可执行命令**：安装后没有 console_scripts 入口，只能 `python -m prism.cli`，AI agent 无法用稳定的短命令调用 | `pyproject.toml` 无 `[project.scripts]` |
| G-2 | **材料旅程/运行状态不可经 CLI 查询**：WebUI 有 `material_journeys`（材料运行、失败、审计），CLI 没有对应只读命令 | `src/prism/webui/journey.py` 存在，`src/prism/cli/main.py` 无 journey/outcome 命令 |
| G-3 | **没有 AI 调用约定文档**：agent 不知道输入/输出契约、退出码语义、长任务行为、脱敏边界 | 仓库无 CLI AI 调用手册 |
| G-4 | **长任务语义未文档化**：`process`/`debate`/`research` 可能耗时数分钟，agent 需要明确的同步/超时/重试预期 | 各命令行为分散，无统一说明 |

### 2.3 核心问题重述

> 一个 AI agent（含知微/Hermes、其他 CLI 编排器）在**零交互、零猜测**的前提下，能否稳定地调用 PRISM 完成：查案例 → 查材料状态 → 摄入/处理材料 → 生成/读取报告 → 导出 PDF → 发起辩论？

---

## 3. 目标与硬边界

### 3.1 目标

1. 安装后存在 `prism` 可执行命令，与 `python -m prism.cli` 行为完全一致；
2. CLI 覆盖 WebUI 已具备的全部**查询**能力（材料旅程、运行状态为必补项）；
3. 全部命令的输出契约、退出码、错误格式、脱敏规则有文档可依；
4. AI agent 调用有明确约定：非交互、JSON、可超时、可重试、可解析。

### 3.2 硬边界（编号 H-1 至 H-5）

| 编号 | 边界 | 含义 |
|---|---|---|
| H-1 | **机器可读优先** | 任何命令输出必须是合法 JSON；禁止彩色 ANSI、进度条、对话式提示污染 stdout |
| H-2 | **非交互** | 命令不读 stdin、不等待用户输入；需要确认的操作要么有显式 flag，要么直接拒绝 |
| H-3 | **脱敏不破** | API key、Cookie、prompt、绝对路径、未脱敏异常链不得出现在任何输出；`[REDACTED]` 规则保持不变 |
| H-4 | **诚实状态** | pending/partial/unknown/failure 不得渲染成 success；空结果与失败是两种状态；退出码如实反映 |
| H-5 | **不引入 WebUI 新工作** | 本文档不为 WebUI 新增任何需求；WebUI 仅存量维护 |

---

## 4. 用户与 AI 调用流程

### 4.1 人用流程（保持现状）

```text
prism cases
prism report-versions --case-id beijing-housing-policy-evolution
prism report-version rv_xxx
prism report-pdf rv_xxx
prism ingest material.md
prism process material.md --case-id ... --case-type policy ...
```

### 4.2 AI agent 调用流程（目标）

```text
AI 判断需要某案例的演变状态
  → prism cases --status active --order updated （stdout JSON，解析 case_id）
  → prism snapshot --case-id X --as-of ...      （结构化快照）
  → 需要新材料 → prism ingest /tmp/policy.pdf --metadata '...'
  → prism process ... --case-id X               （同步等待）
  → prism material-journeys --case-id X         （看材料运行状态，含失败）
  → prism report-versions --case-id X
  → prism report-version rv_xxx --pdf out.pdf   （或 report-pdf）
```

每一步：`stdout` 纯 JSON、`stderr` 错误 JSON、退出码 0/1、无交互、可超时后重试（幂等命令安全）。

---

## 5. 功能需求

优先级：P0 = 本迭代必须交付；P1 = 应交付，可在后续补齐。

### C-1 可执行入口

| 编号 | 需求 | 优先级 |
|---|---|---|
| C-1.1 | 安装包声明 `prism` console script，等价于 `python -m prism.cli` | P0 |
| C-1.2 | `prism --help` 与 `python -m prism.cli --help` 输出一致 | P0 |
| C-1.3 | 入口支持 `PRISM_HOME` 环境变量与默认路径，与现有运行时一致 | P0 |

### C-2 机器可读输出契约

| 编号 | 需求 | 优先级 |
|---|---|---|
| C-2.1 | 成功：stdout 单行 JSON（数组或对象），不夹带其他文本 | P0 |
| C-2.2 | 失败：stderr 单行 JSON `{"error": {"message", "type", ...}}`，退出码非 0 | P0 |
| C-2.3 | 退出码：0=成功；1=执行/运行时错误；2=参数解析错误（argparse 约定） | P0 |
| C-2.4 | 输出 JSON 中 datetime 统一 ISO 8601、路径统一 POSIX 相对路径、敏感字段 `[REDACTED]` | P0 |
| C-2.5 | 任何命令禁止输出 ANSI 色码、进度条、横幅 | P0 |

### C-3 材料旅程与运行状态命令

| 编号 | 需求 | 优先级 |
|---|---|---|
| C-3.1 | `prism material-journeys [--case-id X] [--status committed|failed|pending]`：列出材料运行（material_id、display_name、lifecycle、occurred_at、report_version_id），最新在前 | P0 |
| C-3.2 | `prism material-journey MATERIAL_ID`：单材料七步旅程（staged/ingested/indexed/extracted/merged/graph_written/analyzed）+ run 审计 + 失败详情（stage/error_type/脱敏 message）+ quality 层（mechanism/semantic/gaps） | P0 |
| C-3.3 | `prism outcomes [--status ...]`：直接读持久化 `pipeline_outcomes`（重启后仍可查） | P1 |
| C-3.4 | 上述命令复用 `PrismAPI.material_journeys()/material_journey()/pipeline_outcome()`，不直接访问 SQLite | P0 |

### C-4 长任务与编排语义

| 编号 | 需求 | 优先级 |
|---|---|---|
| C-4.1 | 文档明确各命令典型耗时与"同步等待完成"语义（`process` 等返回即完成，事件驱动路径 `ingest` 返回即入队） | P0 |
| C-4.2 | 幂等命令（`merge-case`/`report-pdf`/`rebuild-report` 同输入）可安全重试，文档列出哪些命令可重试 | P0 |
| C-4.3 | 长时间运行的命令（`process`/`debate`/`research`）在文档中给出建议超时窗口与重试策略 | P1 |

### C-5 AI 调用手册

| 编号 | 需求 | 优先级 |
|---|---|---|
| C-5.1 | 新增 `docs/cli-ai-callable.md`：每个命令的用途、参数、输入输出 JSON 示例、退出码、失败形态、可重试性 | P0 |
| C-5.2 | 手册包含一个端到端 AI 编排示例（查案例→处理材料→看旅程→读报告→导出 PDF） | P0 |
| C-5.3 | 手册明确脱敏边界与"哪些字段永远不会出现" | P0 |

---

## 6. 非目标

- ❌ 为 WebUI 新增任何能力（含新页面、新联动、新汉化）；
- ❌ 交互式 REPL / TUI；
- ❌ 常驻服务 / JSON-RPC 守护进程（本迭代）；
- ❌ 认证 / 远程暴露；
- ❌ 自动重试与指数退避（交给调用方，文档给建议）；
- ❌ 流式输出（debate 等仍一次性返回 JSON）。

---

## 7. 状态与退出码约定

```text
退出码
  0   成功（含"空结果"：如空列表、无失败）
  1   执行错误（运行时、LLM、IO、pipeline 失败）
  2   参数错误（未知子命令、缺必填、非法值）

状态分层（任何命令不得混淆）
  success   有结果或操作完成
  empty     合法空结果（区别于失败）
  partial   机制完成但语义/证据缺口
  failed    失败，错误在 stderr JSON
  unknown   字段缺失（显示 null 或 "unknown"，绝不伪造）
```

---

## 8. 迁移与兼容性

- 现有 21 个子命令的参数与输出**逐字节不变**（新增 `prism` 入口只是等价别名）；
- 新增命令（`material-journeys` 等）只新增，不改旧命令；
- `python -m prism.cli` 路径永久保留；
- 文档更新不改变任何行为。

---

## 9. 验收标准

### A. 入口

- [ ] A1 安装后 `prism --help` 可用，子命令清单与 `python -m prism.cli --help` 一致；
- [ ] A2 在 `PRISM_HOME` 环境变量下 `prism cases` 输出与模块调用一致。

### B. 机器可读

- [ ] B1 全部命令成功输出可被 `json.loads` 单行解析，无杂质文本；
- [ ] B2 触发错误时 stderr 为 JSON、退出码正确（1 或 2）；
- [ ] B3 输出中不存在 ANSI 转义、进度条、prompt 提示。

### C. 材料旅程

- [ ] C1 `material-journeys` 对真实数据返回材料运行列表，失败材料带 lifecycle=failed；
- [ ] C2 `material-journey <id>` 返回七步旅程、run 审计、失败详情（若失败）、quality 层；
- [ ] C3 空列表返回 `[]`（退出码 0），查询错误返回 stderr JSON（退出码 1）。

### D. 文档

- [ ] D1 `docs/cli-ai-callable.md` 覆盖全部命令，含 JSON 示例与退出码；
- [ ] D2 手册的端到端示例可复制执行并得到预期结构。

---

## 10. 完成定义

当以下全部成立时，本迭代才算完成：

- `prism` 命令安装即用，与模块调用等价；
- 材料旅程/运行状态可经 CLI 查询，重启后可查；
- 全部命令输出契约与退出码有文档、有测试、有真实数据验证；
- AI agent 按手册可零交互完成"查案例→处理材料→看状态→读报告→导出 PDF"全链路；
- WebUI 存量不受影响，未新增 WebUI 功能。
