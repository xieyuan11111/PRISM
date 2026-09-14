# PRISM CLI — AI 调用手册

> **版本**：v1.0
> **日期**：2026-09-14
> **配套**：[`cli-ai-callable-requirements.md`](cli-ai-callable-requirements.md)、[`cli-ai-callable-design.md`](cli-ai-callable-design.md)
> **读者**：AI agent（含知微/Hermes）与脚本调用者。人用同一套命令。

## 1. 总则（调用前必读）

### 1.1 三个铁律

```text
1. 非交互：命令不读 stdin、不等待输入、不弹确认。参数给全或报错。
2. 机器可读：成功 → stdout 单行 JSON；失败 → stderr 单行 JSON；绝不混入
   ANSI 色码、进度条、横幅、prompt 提示。
3. 退出码：0=成功（含空结果）；1=执行错误（运行时/LLM/IO/pipeline）；
   2=参数错误（未知子命令、缺必填、非法值）。
```

### 1.2 环境

```bash
export PRISM_HOME=/path/to/prism-home   # 数据目录；不设则用默认路径
prism <subcommand> [args]               # 等价：python -m prism.cli <subcommand> [args]
```

安装（含 CLI 入口）：

```bash
pip install -e .   # 或 uv sync；[project.scripts] 提供 prism 命令
```

### 1.3 输出结构

```text
成功：
  stdout → <单个 JSON 值>，可能为数组（列表命令）或对象（单值命令）
失败：
  stderr → {"error": {"message": "...", "type": "ExceptionName"}}
           （message 已脱敏，不出现 API key / Cookie / prompt / 绝对路径）
```

### 1.4 绝不会出现的字段（脱敏边界）

```text
API key / token / Cookie / 密码 / 凭据值（一律 [REDACTED]）
完整 prompt / 原始异常链中的敏感片段
用户主目录绝对路径（路径一律 PRISM_HOME 相对 POSIX 形式）
```

### 1.5 状态诚实

```text
success / empty / partial / failed / unknown 是五种状态，不得混淆：
  empty     合法空结果（空数组、无失败），退出码 0
  partial   机制完成但语义/证据有缺口（如 semantic_status="partial"）
  unknown   字段缺失（null 或 "unknown"），绝不伪造为成功
  failed    失败，stderr JSON + 退出码 1
```

## 2. 命令速查表

| 命令 | 用途 | 典型耗时 | 可安全重试 |
|---|---|---|---|
| `search` | 检索证据 | <10s | ✅ |
| `cases` | 列案例 | <10s | ✅ |
| `timeline` | 案例时间线 | <10s | ✅ |
| `state` | 历史时点状态 | <10s | ✅ |
| `snapshot` | GTI 快照 | <10s | ✅ |
| `compare` | 双时点比较 | <10s | ✅ |
| `ingest` | 摄入文件并入队 | <30s | ✅（同文件按 hash 幂等） |
| `process` | 同步跑完整管线 | 30–180s+ | ✅（幂等） |
| `merge-case` | 账本重建案例 | <60s | ✅ |
| `bind-material` | 绑定材料到案例 | <60s | ✅ |
| `report` | 渲染报告 JSON | <60s | ✅ |
| `report-versions` | 列报告版本 | <10s | ✅ |
| `report-version` | 读报告版本 | <10s | ✅ |
| `report-pdf` | 导出 PDF | 10–60s | ✅（同输入同路径幂等） |
| `add-material` | 追加材料并重算报告 | 30–180s+ | ✅（幂等） |
| `rebuild-report` | 重建报告版本 | 30–180s+ | ✅ |
| `debate` | 多视角辩论 | 60–600s | ⚠️ 消耗 LLM 额度，先确认失败再重试 |
| `follow-up` | 视角追问 | 30–120s | ⚠️ 同上 |
| `adjudication-history` | 仲裁审计 | <10s | ✅ |
| `fetch` / `fetch-all` | 抓取白名单源 | 10–120s | ⚠️ 网络操作，先看失败原因 |
| `discover` | 源发现 | 10–120s | ⚠️ 同上 |
| `research` | 研究计划执行 | 300–900s | ⚠️ 消耗额度 |
| `material-journeys` | 列材料运行状态 | <10s | ✅ |
| `material-journey` | 单材料七步旅程 | <10s | ✅ |

## 3. 重点命令参考（含真实示例）

> 示例来自真实 `PRISM_HOME`（北京房地产政策演变案例），字段截断仅为了排版，真实输出为完整 JSON。

### 3.1 `cases`

```bash
prism cases [CASE_ID] [--type TYPE] [--status STATUS] [--order case_id|last_updated|latest_observed] [--reverse] [--unresolved-only]
```

`CASE_ID` 为可选位置参数（缺省列全部案例）。

```json
[
  {
    "case_id": "beijing-housing-policy-evolution",
    "case_type": "policy",
    "name": "Beijing housing and housing-provident-fund policy evolution",
    "status": "active",
    "material_count": 4,
    "latest_observed_at": "2026-09-02T00:37:10.541284+00:00",
    "has_unresolved_gaps": true,
    "has_unresolved_conflicts": false
  }
]
```

### 3.2 `material-journeys`（新增）

```bash
prism material-journeys [--case-id X] [--status committed|failed|pending]
```

```json
[
  {
    "material_id": "mat_b5f7296bf770bb6c59b9015b",
    "display_name": "住房公积金如何稳定楼市：UBS研报分析",
    "case_id": "beijing-housing-policy-evolution",
    "lifecycle_status": "committed",
    "ui_status": "成功",
    "occurred_at": "2026-09-05T03:25:53.361383+00:00",
    "mechanism_status": "pass",
    "semantic_status": "unknown",
    "evidence_gap_count": null,
    "failed_stage": null,
    "error_type": null
  }
]
```

`--status failed` 时 `lifecycle_status="failed"`、`failed_stage`/`error_type` 有值、`ui_status="失败"`。空结果返回 `[]`（退出码 0）。

### 3.3 `material-journey`（新增）

```bash
prism material-journey mat_b5f7296bf770bb6c59b9015b
```

```json
{
  "material_id": "mat_b5f7296bf770bb6c59b9015b",
  "lifecycle_status": "committed",
  "steps": [
    {"step": "staged", "status": "unknown"},
    {"step": "ingested", "status": "completed"},
    {"step": "indexed", "status": "unknown"},
    {"step": "extracted", "status": "unknown"},
    {"step": "merged", "status": "completed"},
    {"step": "graph_written", "status": "unknown"},
    {"step": "analyzed", "status": "unknown"}
  ],
  "run": {"status": "completed", "stages": []},
  "failure": {"stage": null, "error_type": null, "message": null, "failed_at": null},
  "quality": {"mechanism_status": "pass", "semantic_status": "unknown", "evidence_gap_count": null}
}
```

注意：`unknown` 表示**没有持久化审计证据**（如实），不是失败。未知 `material_id` → stderr JSON + 退出码 1。

### 3.4 `report-versions` / `report-version`

```bash
prism report-versions [CASE_ID] [--as-of ISO8601]
prism report-version VERSION_ID
prism report-version VERSION_ID --pdf reports/exports/out.pdf   # 直接导 PDF
```

`CASE_ID` 为可选位置参数。`report-versions` 返回数组（含 `version_id/case_id/as_of/created_at/trigger/summary_origin/input_hash/markdown_hash/parent_version_id`）；`report-version` 额外含完整 `markdown` 正文。

```json
[
  {
    "version_id": "rv_8da72f8620aa48a1b72da3f6fe7ae4c848f8e6e34bdeca15997bed141a4a6663",
    "case_id": "beijing-housing-policy-evolution",
    "as_of": "2026-09-03T00:00:00+00:00",
    "created_at": "2026-09-05T03:25:53.570408+00:00",
    "trigger": "initial",
    "summary_origin": "fallback",
    "parent_version_id": null
  }
]
```

### 3.5 `report-pdf`

```bash
prism report-pdf VERSION_ID OUTPUT_PATH
```

`OUTPUT_PATH` 为位置参数，必须相对 `PRISM_HOME/output`（如 `reports/exports/rv_xxx.pdf`），禁止 `..` 与绝对路径（安全设计）。同输入同输出路径幂等；内容冲突拒绝覆盖。

### 3.6 `ingest` / `process`

```bash
prism ingest /tmp/policy.pdf --metadata '{"case_tags":["housing"]}'
prism process /tmp/policy.pdf --case-id beijing-housing-policy-evolution
prism process /tmp/policy.pdf --case-json '{"case_id":"housing-2026","case_type":"policy","canonical_name":"2026年房地产政策","start_at":"2026-01-01T00:00:00+00:00","status":"active"}'
```

- `ingest`：摄入并索引，入队自动管线，**立即返回**（不等待抽取完成）；
- `process`：**同步等待**整条管线完成，返回含 pipeline run、case outcome、warnings 的完整结果；失败抛结构化错误。
- `process` 的目标案例二选一：`--case-id`（已记录案例，从账本加载，防漂移）或 `--case-json`（内联声明 case_id/case_type/canonical_name/start_at/status）。

## 4. 端到端编排示例（可复制执行）

```bash
export PRISM_HOME=/path/to/prism-home

# 1. 查案例
prism cases --status active
# → 取 case_id

CASE=beijing-housing-policy-evolution

# 2. 处理一份新材料（同步等待；案例已记录时用 --case-id）
prism process ./materials/policy.md --case-id "$CASE"
# 新案例则用 --case-json 内联声明
# prism process ./materials/policy.md --case-json '{"case_id":"housing-2026","case_type":"policy","canonical_name":"2026年房地产政策","start_at":"2026-01-01T00:00:00+00:00","status":"active"}'
# → 成功输出含 material_id / case_outcome / report_version

# 3. 看材料运行状态（含失败）
prism material-journeys --case-id "$CASE"
prism material-journey mat_xxx

# 4. 列报告并读取（CASE_ID 是位置参数）
prism report-versions "$CASE"
VID=$(prism report-versions "$CASE" | python -c "import sys,json; print(json.load(sys.stdin)[0]['version_id'])")
prism report-version "$VID"

# 5. 导出 PDF（OUTPUT_PATH 为位置参数，相对 output 目录）
prism report-pdf "$VID" "reports/exports/$VID.pdf"
```

每一步的 stdout 都可以直接 `python -c "import sys,json; json.load(sys.stdin)"` 验证。

## 5. 长任务与重试建议

```text
process      建议超时 180s；真实 LLM 抽取更长，失败后可安全重试（幂等）
add-material 建议超时 180s；幂等
debate       建议超时 600s；重试会重新消耗 LLM 额度，先确认上次确实失败
research     建议超时 900s；同上
其余命令     通常 <10s

通用规则：退出码 0 后不要重试；退出码 1 先读 stderr JSON 的 error.type，
          幂等命令可直接重试，额度/网络类命令先确认失败原因。
```

## 6. 常见失败形态

| 场景 | stderr | 退出码 |
|---|---|---|
| 未知子命令 / 缺必填 / 非法枚举 | `{"error":{"type":"usage"}}` | 2 |
| 材料不存在 | `{"error":{"type":"LookupError"}}` | 1 |
| 案例不存在 / 未绑定 | `{"error":{"type":"LookupError"}}` | 1 |
| 报告版本不存在 | `{"error":{"type":"LookupError"}}` | 1 |
| PDF 路径非法 | `{"error":{"type":"ReportPdfPathError"}}` | 1 |
| LLM provider 不可用 | `{"error":{"type":"..."}}` | 1 |
| 后台管线失败 | `{"error":{"count":N,"message":"automatic pipeline subscriber ..."}}` | 1 |

## 7. 与 WebUI 的关系

WebUI（`python -m prism.webui`，端口 8765）与 CLI 共享同一 `PrismAPI` 与同一 `PRISM_HOME`。CLI 是**持续投入**的一等界面；WebUI 保留既有功能但不再新增。两者可同时读取同一数据；写操作都走同一管线与账本。

## 8. 安全边界重申

```text
不读取 stdin；不执行 shell 注入（参数走 argparse，不拼 shell 字符串）
输出永远 JSON；敏感字段 [REDACTED]；路径相对化
不请求凭据；不绕过登录墙/付费墙/反爬
只操作 PRISM_HOME 下数据；PDF 输出限制在 output 目录内
```
