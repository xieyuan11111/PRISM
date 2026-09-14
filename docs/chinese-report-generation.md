# PRISM 中文报告能力 — 实现说明

> **日期**：2026-09-14
> **目标**：报告正文、LLM 摘要、PDF 标题/校验统一生成中文；不以事后机器翻译替代证据约束的报告生成。

## 问题

现有报告仍以英文为默认语言，来源包括：

1. `ReportService._render_markdown()` 的确定性 fallback 模板；
2. `summarize_report` 的 LLM prompt 未声明目标语言；
3. PDF 标题、必需章节文字与回读验证只接受英文；
4. `ReportVersionLedger.input_hash()` 未包含语言，因此同一分析生成不同语言报告可能错误复用版本。

WebUI 汉化不改变报告正文。

## 目标 API

```text
默认语言：zh-CN
可选：en

prism report CASE_ID [--lang zh-CN|en] [--save]
prism rebuild-report CASE_ID [--lang zh-CN|en]
```

`PrismAPI.report_case/save_report_version/rebuild_report` 新增 keyword-only `language: str = "zh-CN"`。

## 硬约束

- 报告语言必须进入 `input_hash`，中文和英文版本独立、均不可变；
- 旧版本/旧数据库兼容：旧行读取时 language 默认 `en` 或 `unknown_legacy`，不得误标中文；
- LLM 摘要只基于已有分析/证据，语言变化不得放宽 citation/time/source/case 校验；
- fallback、LLM summary、章节标题、PDF `<html lang>`、metadata 与 PDF 回读校验必须同语言；
- case_id/source_id/episode_key/trigger 枚举、原始引文不得翻译；
- `partial/unknown/failure` 状态词按中文输出但不伪装成功；
- 不新增 WebUI 功能；WebUI 存量只显示报告既有语言。

## 中文术语表（固定）

| English | 中文 |
|---|---|
| Evolution Report | 演变报告 |
| Case ID | 案例 ID |
| Case type | 案例类型 |
| Recorded status | 记录状态 |
| As of | 截至时间 |
| Executive Summary | 执行摘要 |
| Timeline Stages | 时间线阶段 |
| Invalidated Facts | 已失效事实 |
| Revision and Conflict Relations | 修订与冲突关系 |
| Turning Points | 关键转折点 |
| Change Reasons | 变化原因 |
| Evidence Gaps | 证据缺口 |
| Open Questions | 待解问题 |
| Citations | 引用与证据定位 |
| deterministic fallback | 确定性回退 |
| model-distilled | 模型提炼 |

## 实现拆分

### ZCode：报告核心与版本语言

允许文件：

```text
src/prism/report/service.py
src/prism/report/models.py
src/prism/report/ledger.py
src/prism/api/facade.py
src/prism/cli/main.py
src/prism/cli/__init__.py（如需）
tests/test_report*.py
tests/test_api*.py
tests/test_cli*.py
```

实现：语言验证/传递、中文 fallback、LLM prompt 的语言指令、input hash/ledger language 字段与迁移、CLI `--lang`。

### Codex：PDF 国际化 + 回读验证（仅在独立 worktree）

允许文件：

```text
src/prism/report/pdf.py
tests/test_report_pdf.py
```

实现：中英文 required sections/title/as-of 校验、`<html lang>`/metadata、CJK PDF 回读，保持旧英文 PDF 测试兼容。

### 集成

主助手统一审阅、解决 ReportVersion schema/ledger 合并冲突、运行全套报告/CLI/PDF 回归，使用真实 HN-AD 案例生成 `zh-CN` 报告与 PDF，并验证内容确为中文。

## 验收

1. `prism report stress-tolerant-hnad-strains-2026 --lang zh-CN --save --no-llm`：Markdown 标题和所有模板章节中文；case/source/quote 原样；
2. `--lang en` 生成独立英文版本，二者 version_id/input_hash 不同；
3. LLM summarize_report 时 prompt 明确中文，返回摘要中文；无 LLM 时 fallback 中文；
4. `prism report-pdf <中文版本> reports/exports/hnad-zh.pdf`：PDF 回读标题/章节中文且包含 W30；
5. 旧英文报告读回/导出不回归；
6. report/ledger/api/cli/pdf 全套测试、ruff、compile、diff check 通过。
