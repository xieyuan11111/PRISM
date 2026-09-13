# PRISM CLI 优先 + AI 可调用 — 技术设计文档

> **版本**：v1.0（待实现评审）
> **日期**：2026-09-14
> **对应需求**：[`docs/cli-ai-callable-requirements.md`](cli-ai-callable-requirements.md)
> **适用范围**：CLI 入口、材料旅程命令、输出契约、AI 调用手册

## 1. 设计目标与原则

把 PRISM CLI 做成 AI agent 可以零交互调用的稳定工具。原则：

1. `prism` 与 `python -m prism.cli` 是同一程序的两种叫法，行为逐字节一致；
2. 全部输出 JSON，stdout 只承载结果，stderr 只承载错误，退出码语义稳定；
3. 新材料状态查询复用既有 `PrismAPI`（WebUI 旅程的同一数据源），不新建事实库、不直接访问 SQLite；
4. 既有 21 个子命令的行为、参数、输出**不变**；
5. 脱敏、诚实状态、非交互等既有边界保持不变。

## 2. 现有实现核对

### 2.1 已存在且应复用的机制

| 能力 | 位置 | 说明 |
|---|---|---|
| JSON 编码与脱敏 | `src/prism/cli/main.py::_jsonable/_write_json/_safe_error_message` | dataclass/Mapping/日期/路径统一编码；敏感键 `[REDACTED]` |
| 错误 JSON + 退出码 | `src/prism/cli/main.py::main` | 成功 `_write_json(result, stdout)`；异常 `{"error":{...}}` 到 stderr，返回 1；argparse 解析失败返回 2 |
| 注入式 facade | `main(argv, api=...)` | 测试与嵌入共用 |
| 材料旅程视图 | `PrismAPI.material_journeys()/material_journey()` + `src/prism/webui/journey.py::journey_view_data` | 数据在 facade 层已齐备 |
| 持久化运行结果 | `pipeline_outcomes`（`PrismAPI.pipeline_outcome()`） | 重启后可查 |

### 2.2 缺口

1. `pyproject.toml` 无 `[project.scripts]`，无 `prism` 命令；
2. CLI 无 `material-journeys` / `material-journey` / `outcomes` 命令；
3. 无 AI 调用手册。

## 3. 实现方案

### 3.1 `prism` 可执行入口（C-1）

`pyproject.toml`：

```toml
[project.scripts]
prism = "prism.cli:main_entry"
```

`src/prism/cli/__init__.py`（或 `__main__.py`）新增同步入口：

```python
def main_entry() -> int:
    """Console-script entry: run the async CLI main and exit with its code."""
    import asyncio
    from prism.cli.main import main as async_main
    return asyncio.run(async_main())
```

行为要求：

- `prism --help` 与 `python -m prism.cli --help` 完全一致（同一个 `build_parser()`）；
- 退出码直接透传 `main()` 返回值；
- 不捕获 `asyncio.run` 之外的异常；`main()` 已统一兜底为 stderr JSON + 1。

### 3.2 材料旅程命令（C-3）

在 `src/prism/cli/main.py` 新增三个 handler 与 argparse 子命令：

```text
prism material-journeys [--case-id X] [--status committed|failed|pending]
prism material-journey MATERIAL_ID
prism outcomes [--status committed|failed]        # P1
```

实现要点：

1. `handle_material_journeys` 调用 `await api.material_journeys(case_id=..., status=...)`，返回 JSON-safe 行列表（`material_id/display_name/lifecycle/occurred_at/report_version_id`），最新在前（复用 facade 顺序，不重排）；
2. `handle_material_journey` 调用 `await api.material_journey(material_id)`，投影为：

```json
{
  "material_id": "...",
  "display_name": "...",
  "case_id": "...",
  "raw_path": "raw/...",
  "corpus_path": "corpus/...",
  "lifecycle_status": "committed|failed|pending",
  "occurred_at": "ISO 8601",
  "steps": [
    {"step": "staged", "label": "上传(staged)", "status": "unknown", "detail": "...", "time": null},
    {"step": "ingested", "status": "completed|unknown", "time": "..."},
    {"step": "indexed", "status": "completed|skipped|unknown"},
    {"step": "extracted", "status": "completed|skipped|unknown"},
    {"step": "merged", "status": "completed|skipped|unknown"},
    {"step": "graph_written", "status": "completed|skipped|unknown"},
    {"step": "analyzed", "status": "completed|unknown"}
  ],
  "run": {"status": "...", "stages": [{"name": "index|extract|graph", "status": "...", "detail": null}]},
  "failure": {"stage": null, "error_type": null, "message": null, "failed_at": null},
  "quality": {"mechanism_status": "unknown", "semantic_status": "unknown", "evidence_gap_count": null}
}
```

3. **复用而非复制**：`src/prism/webui/journey.py::journey_view_data` 已是 JSON-safe 投影（含七步状态推导、脱敏失败、quality 层）。CLI handler 应复用该纯函数（`from prism.webui.journey import journey_view_data`），而不是在 CLI 里重写状态机。若担心 CLI 依赖 webui 模块，可把 `journey_view_data` 提升到中立位置（如 `prism/api/views.py`）再被两端引用——**推荐后者**，避免 CLI→webui 的依赖方向；
4. `handle_outcomes`（P1）调用 `api.pipeline_outcome(material_id)` 的批量版或直接 `api.pipeline` 只读方法；若 facade 无批量 outcomes，则本迭代只实现 `material-journeys`（其数据源已含终态），`outcomes` 延后；
5. 参数校验：`--status` 枚举校验（committed/failed/pending），非法值在 handler 内抛 `ValueError` → stderr JSON + 退出码 1（与既有命令一致）；`--case-id` 空串拒绝；
6. 空结果：返回 `[]`（退出码 0），绝不返回错误。

### 3.3 输出契约固化（C-2 / C-4）

1. 在 `main()` 的 argparse 构建处补充 `--json` 兼容说明文档（不新增开关——CLI 本就全 JSON；`--json` 只作为显式声明可接受，行为无差异，可选）；
2. `docs/cli-ai-callable.md` 中给出退出码表与"可重试命令"清单：

```text
可安全重试（幂等）：
  search / cases / timeline / state / snapshot / compare /
  material-journeys / material-journey / report / report-versions /
  report-version / report-pdf（同输入同输出路径）/ rebuild-report /
  merge-case / bind-material / adjudication-history

不可盲目重试：
  ingest（同文件重复摄入会按 hash 幂等，可重试但会重复入队）
  process（幂等，可重试）
  add-material（幂等，可重试）
  debate / follow-up / research（消耗 LLM 配额，重试前确认上次确实失败）
  fetch / fetch-all / discover（网络操作，重试前确认上次失败原因）
```

3. 长任务建议超时（写入手册）：

```text
process        默认建议 180s；真实 LLM 抽取可能更长，失败后重试
debate         建议 600s（4 视角一轮）；超时后重试会重新消耗额度
research       建议 900s；超时后重试
其余命令       通常 < 10s
```

### 3.4 AI 调用手册（C-5）

新增 `docs/cli-ai-callable.md`，结构：

```text
1. 总则：非交互 / JSON / 退出码 / 脱敏
2. 快速上手：安装 + PRISM_HOME + 第一个命令
3. 命令参考（每个命令）：
   名称 / 用途 / 参数表 / JSON 输入输出示例 / 失败形态 / 可重试性
4. 端到端编排示例（可复制执行）：
   查案例 → 处理材料 → 看旅程 → 读报告 → 导出 PDF
5. 边界与安全：不会出现的字段、脱敏规则、配额注意
```

### 3.5 文件边界

```text
src/prism/cli/main.py        （+3 handler、+3 子命令）
src/prism/cli/__init__.py     （+main_entry，如 __init__ 无 asyncio 依赖）
pyproject.toml                （+[project.scripts]）
src/prism/api/views.py        （新建，journey_view_data 等中立投影；如选推荐方案）
src/prism/webui/journey.py    （改为引用中立视图，保持行为不变）
docs/cli-ai-callable.md       （新建手册）
docs/cli-ai-callable-requirements.md / -design.md（本文档）
tests/test_cli_material_journey.py（新建）
tests/test_cli.py             （+入口与契约测试）
```

## 4. 测试矩阵

### 单元 / 契约测试

- `prism` 入口：`main_entry()` 返回 `main()` 退出码；`--help` 与模块调用一致（对比 stdout）；
- `material-journeys`：注入 fake facade 返回真实 `MaterialJourneyView` 列表；验证 JSON 行字段、空列表 `[]`、`--status` 过滤、非法 status 拒绝；
- `material-journey`：七步投影正确（复用 `journey_view_data`）、失败材料含 failure 详情、未知材料抛 `LookupError` → stderr JSON + 1；
- 输出纯净性：成功 stdout 可 `json.loads` 且无 ANSI；错误 stderr 为 JSON 对象含 `error`；
- 退出码：0 / 1 / 2 三态分别覆盖；
- 兼容性：既有 21 个命令的 argparse 注册数量不变、既有测试全绿。

### 真实数据验收

```bash
export PRISM_HOME='C:/Users/xieyu/.prism'
prism cases
prism material-journeys --case-id beijing-housing-policy-evolution
prism material-journey <material_id>
prism report-versions --case-id beijing-housing-policy-evolution
prism report-pdf <version_id> --output reports/exports/<id>.pdf
```

每条命令 stdout 必须能被 `python -c "import sys,json; json.load(sys.stdin)"` 通过。

## 5. 迁移与兼容性

- 不改变既有 21 命令的任何行为（新增命令只增不减）；
- `python -m prism.cli` 路径永久保留；
- 若提升 `journey_view_data` 到中立模块，`prism.webui.journey` 保持导出兼容（re-export），WebUI 测试不受影响；
- 新表、新 schema、新事实库一概不引入。

## 6. 分阶段实施

### Phase 1（P0）：入口 + 旅程命令

1. `pyproject.toml` 加 `[project.scripts]`；`__init__.py` 加 `main_entry`；
2. 提升 `journey_view_data` 到中立模块（或 CLI 复用 webui 模块，二选一，推荐前者）；
3. 新增 `material-journeys` / `material-journey` handler + 子命令；
4. 测试：入口一致性、旅程命令、输出纯净性、退出码三态；
5. 真实数据验收 + 既有测试全绿。

### Phase 2（P0）：AI 调用手册

1. 撰写 `docs/cli-ai-callable.md`（全部命令 + 示例 + 退出码 + 可重试性 + 超时建议）；
2. 端到端编排示例用真实数据跑通后写入手册。

### Phase 3（P1）：outcomes 命令与 `--json` 显式声明

1. `prism outcomes`（如 facade 提供批量只读 outcomes）；
2. `--json` 开关（行为无差异，仅为显式声明，可选实现）。

## 7. 完成定义

- `prism` 安装即用，与模块调用逐字节一致；
- 材料旅程/运行状态 CLI 可查，真实数据验收通过；
- 手册覆盖全部命令且示例可执行；
- 既有 21 命令与 WebUI 存量零回归；
- 输出纯净性、退出码、脱敏均有测试守护。
