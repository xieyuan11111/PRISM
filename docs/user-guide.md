# PRISM（棱镜）用户指南

> 面向第一次使用 PRISM 的人。读完这份文档，你应该能：安装 PRISM、放入一份材料、跑出演变报告、导出 PDF。
> 本指南对应 CLI 为主的当前版本（WebUI 为可选）。

---

## 1. PRISM 是什么

PRISM 是一个**政策 / 学术观点 / 公共议题演变追踪工具**。

它做的不是"把一堆新闻总结成一篇文章"，而是回答这一类问题：

```text
- 这个政策是怎么一步步变成现在这样的？
- 2026 年 1 月的时候，大家知道什么、不知道什么？
- 这个观点是谁先提出、被谁反驳、后来为什么变了？
- 每个结论背后，是哪份原始材料、哪一段原文在支撑？
```

### 核心概念（先认识四个词）

| 概念 | 一句话 |
|---|---|
| **案例（Case）** | 你追踪的对象，比如"北京房地产政策演变" |
| **材料（Material）** | 喂给 PRISM 的原始资料：政策原文、新闻、论文、PDF |
| **报告（Report）** | 系统生成的演变分析，结论都带证据引用 |
| **证据（Evidence）** | 报告结论回溯到的原文位置（来源文件 + 段落 + 引文） |

### PRISM 不是什么

- ❌ 不是新闻阅读器 / 摘要器；
- ❌ 不替你下政治、投资、法律结论；
- ❌ 不编造证据——没有原文支撑的结论会如实标为"证据缺口"或"未知"。

---

## 2. 安装

要求：Python 3.11 或更高。

```bash
# 克隆仓库
git clone git@github.com:xieyuan11111/PRISM.git
cd PRISM

# 安装（默认离线；CLI 是核心，无需额外可选依赖）
pip install -e .

# 可选：要真实调用大模型（LLM）抽取/提炼时
pip install -e ".[openai-sdk]"

# 可选：要导出 PDF 时（需要系统已装 Edge 或 Chromium）
pip install -e ".[pdf]"

# 可选：要打开 WebUI 时
pip install -e ".[webui]"
```

安装后应出现 `prism` 命令：

```bash
prism --help
```

### 数据目录

PRISM 把所有数据（原始材料、语料、索引、报告）放在一个目录里，通过环境变量指定：

```bash
# Linux / macOS
export PRISM_HOME="$HOME/prism-home"

# Windows (cmd)
set PRISM_HOME=%LOCALAPPDATA%\prism-home

# Windows (Git Bash)
export PRISM_HOME="$LOCALAPPDATA/prism-home"
```

不设置时使用默认路径。**所有命令都读取同一个 `PRISM_HOME`，CLI 和 WebUI 看到的是同一份数据。**

---

## 3. 快速开始（5 分钟）

```bash
export PRISM_HOME="$HOME/prism-home"

# ① 看当前有哪些案例
prism cases

# ② 处理一份材料（放到一个已存在的案例里；会同步等待管线完成）
prism process ./材料.md --case-id beijing-housing-policy-evolution

# ③ 看这份材料跑得怎么样（含失败）
prism material-journeys --case-id beijing-housing-policy-evolution
prism material-journey <材料ID>

# ④ 列出并读取报告
prism report-versions beijing-housing-policy-evolution
prism report-version <版本ID>

# ⑤ 导出 PDF
prism report-pdf <版本ID> reports/exports/<版本ID>.pdf
```

> 第一次没有案例怎么办？用 `--case-json` 内联声明一个新案例：

```bash
prism process ./材料.md --case-json '{"case_id":"my-policy-2026","case_type":"policy","canonical_name":"我的案例","start_at":"2026-01-01T00:00:00+00:00","status":"active"}'
```

---

## 4. 常用命令

所有命令输出 **JSON**（给程序/AI 用很方便），退出码 0=成功、1=执行错误、2=参数错误。

### 查看

| 想做什么 | 命令 |
|---|---|
| 列案例 | `prism cases` |
| 看案例时间线 | `prism timeline <case_id> --as-of 2026-09-01T00:00:00+00:00` |
| 看某历史时点的状态 | `prism state <case_id> --cutoff-at 2026-06-01T00:00:00+00:00` |
| 比较两个时点变化 | `prism compare <case_id> --earlier 2026-01-01T00:00:00+00:00 --later 2026-09-01T00:00:00+00:00` |
| 检索证据 | `prism search "关键词"` |
| 看材料运行状态 | `prism material-journeys` / `prism material-journey <id>` |
| 列报告版本 | `prism report-versions <case_id>` |

### 录入与处理

| 想做什么 | 命令 |
|---|---|
| 摄入文件（入队，不等待） | `prism ingest 文件.md` |
| 处理材料（同步等待完成） | `prism process 文件.md --case-id <案例ID>` |
| 重建一个案例的报告 | `prism rebuild-report <case_id>` |

### 报告

| 想做什么 | 命令 |
|---|---|
| 渲染报告为 JSON | `prism report <case_id>` |
| 读某个版本 | `prism report-version <版本ID>` |
| 导出 PDF | `prism report-pdf <版本ID> <输出路径>` |
| 直接导出并看 | `prism report-version <版本ID> --pdf <输出路径>` |

### 时间格式

时间参数统一用**带时区的 ISO 8601**，例如：

```text
2026-06-01T00:00:00+00:00
```

不带时区会被拒绝（防止歧义）。

---

## 5. WebUI（可选）

CLI 之外，PRISM 带一个本机 Web 界面（只监听本机，不开放远程）：

```bash
python -m prism.webui
# 然后浏览器打开 http://127.0.0.1:8765
```

WebUI 提供：案例主页 / 材料上传与旅程 / 报告列表与阅读 / 证据检索 / 辩论剧场。

> WebUI 与 CLI 共用同一个 `PRISM_HOME`，看到的是同一份数据。

---

## 6. 完整例子：追踪一个政策

假设要追踪"住房公积金条例修改"：

```bash
export PRISM_HOME="$HOME/prism-home"

# 1) 放入三份材料（政策原文、新闻、研报）
prism process 条例修改决定.md --case-json '{"case_id":"provident-fund-2026","case_type":"policy","canonical_name":"住房公积金条例修改","start_at":"2026-01-01T00:00:00+00:00","status":"active"}'
prism process 官方解读.md     --case-id provident-fund-2026
prism process 机构研报.pdf    --case-id provident-fund-2026

# 2) 看演变时间线（--as-of 必填）
prism timeline provident-fund-2026 --as-of 2026-09-01T00:00:00+00:00

# 3) 看某个历史时点大家知道什么（--cutoff-at）
prism state provident-fund-2026 --cutoff-at 2026-03-01T00:00:00+00:00

# 3b) 比较两个时点之间的变化
prism compare provident-fund-2026 --earlier 2026-03-01T00:00:00+00:00 --later 2026-09-01T00:00:00+00:00

# 4) 生成并导出报告
prism report-versions provident-fund-2026
prism report-pdf <版本ID> reports/exports/provident-fund.pdf
```

报告里每个关键结论都会指向原始材料的具体段落和引文；缺失的部分如实显示为"证据缺口"，不会编造。

---

## 7. 常见问题（FAQ）

**Q：为什么我看到的材料状态是 "unknown"？**
A：表示系统没有持久化的审计记录能证明那一步完成。这不是失败，是如实显示——系统不会把没验证的步骤标成成功。

**Q：`semantic=partial` 是什么意思？**
A：机制链路（摄入/索引/抽取/图谱）完成，但大模型提炼的语义质量还有缺口或未提炼。它是中间状态，不是成功也不是崩溃。

**Q：没有配置大模型能跑吗？**
A：能。默认离线运行，摄入、索引、时间线、报告（确定性回退摘要）都可用；真实抽取/提炼需要 `openai-sdk` extra 和 provider 配置。

**Q：报告怎么导出 PDF？**
A：`prism report-pdf <版本ID> <相对路径>`。输出路径必须相对 `PRISM_HOME/output`（安全限制），且需要 `pdf` extra 与 Edge/Chromium。

**Q：密钥怎么配置？**
A：API key 只从本地环境变量读取，绝不写入代码、配置、语料、报告或日志。

---

## 8. 更多文档

```text
README.md                       项目总览与安装
docs/cli-ai-callable.md         CLI 命令完整参考（含 AI 编排示例）
docs/webui-getting-started.md   WebUI 从零起步
docs/webui-workbench-requirements.md / -design.md   WebUI 需求与设计
```

---

*PRISM 是独立开源项目。它追踪证据，不替你下结论；它如实展示缺口，不假装一切完美。*
