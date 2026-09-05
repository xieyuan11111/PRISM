# PRISM v1 发布状态

**状态更新时间：** 2026-09-06
**发布目标：** v1 初步产品 / 本机单用户 WebUI 版本

## 1. 总体判断

```text
核心工程：基本完成
真实 Graphiti 机制链：已通过
真实 provider 机制链：已实际运行
WebUI 功能骨架：已完成
LLM 语义稳定性：仍需保持 semantic-partial 的诚实边界
正式发布收口：未完成
```

当前不能称为已经发布的 v1，因为文档同步、WebUI 最终冒烟、开源治理、CI、lint、依赖可复现和安全扫描尚未全部完成。

## 2. 状态分类

| 状态 | 含义 |
|---|---|
| `implemented` | 代码与离线测试已存在 |
| `live-mechanism-pass` | 真实服务或 provider 的机制链已通过 |
| `semantic-partial` | 有真实输出，但仍存在语义缺口或跨运行波动 |
| `experimental` | 有显式开关和实验数据，不是默认生产路径 |
| `deferred` | 明确延后到 v1.1 或更后 |
| `not-yet-verified` | 尚未完成所需的真实验收 |

## 3. 已完成状态

| 项目 | 当前状态 | 说明 |
|---|---|---|
| 材料摄入与 Markdown 正本 | implemented | Markdown、PDF、OCR、raw/corpus 与 frontmatter |
| SQLite / FTS5 | implemented | 可重建索引与证据定位 |
| 自动 pipeline | implemented | index → extract → case ledger → graph write |
| 时序模型 | implemented | happened / valid / invalid / observed 时间轴 |
| 历史 cutoff / snapshot / compare | implemented | 确定性时间边界与未来材料隔离 |
| M2 自动辩论 | implemented | 多视角、交叉质询、综合、追问与审计 |
| 报告版本与 PDF | implemented | Markdown 正本派生 PDF |
| NiceGUI 页面 | implemented | `/`、`/debate`、`/evidence`、`/materials` |
| PRISM 自有 LLM transport | implemented | 统一使用官方 OpenAI Python SDK，lazy import |
| Graphiti 合成 live spike | live-mechanism-pass | 原生 Neo4j/Graphiti 写入、重启、cutoff、幂等 |
| 真实 provider + 真实材料 + Graphiti | live-mechanism-pass | 已完成端到端机制验收；语义质量另行判定 |
| prompt profiles | experimental | baseline、protocol-v1、protocol-v2 |
| split-v1 | experimental | Flash 单材料三轮核心 node/fact 稳定；第二政策与学术案例复现均为 unstable，baseline 继续默认 |

## 4. 尚未完成的 v1 收口项

```text
工程文档与实际状态完全同步
WebUI 从零安装/配置/使用说明完善
WebUI 最终本机真实冒烟
一个完整生命周期案例验收
CONTRIBUTING.md
CHANGELOG.md
CODE_OF_CONDUCT.md
SECURITY.md
.github/workflows/ci.yml
ruff 配置与 lint gate
依赖可复现策略
最终密钥/路径/身份扫描
v1 RC 报告与版本整理
```

## 5. 真实语义边界

当前真实 LLM 抽取不应被表述为稳定完成：

```text
accepted evidence/source coverage 可达到 100%
core node/fact 在指定 Flash split-v1 单材料实验中出现稳定交集
第二政策和学术案例跨运行稳定性不足，claims 与 relations 仍需更多案例验证
provider 仍可能产生候选级校验 gap 或 JSON envelope failure
semantic-partial 不等于系统机制失败
```

处理原则：

```text
不放宽 quote/time/source/case/relation 校验
不人工制造 relation
不以节点数量判定 prompt 优劣
失败保留为 audit / gap
```

## 6. 明确延后的 v1.1 项目

```text
完整多案例交互工作台
持续来源采集平台
Graphiti 正式分页与大规模性能优化
实时辩论流与任务控制
模型设置页面
认证、远程访问和多用户权限
Docker / Docker Compose
```

## 7. 当前产品定义

当本计划全部完成后，PRISM v1 的最终产品定义是：

> 一个本机单用户、loopback-only、通过 WebUI 操作的可审计政策与学术观点演变追踪工具。

用户能够：

```text
选择案例
查看时间线与历史状态
检索证据
追加材料
运行真实 LLM 抽取
查看 mechanism / semantic / evidence gap
发起多视角辩论
追问视角
保存报告
导出 PDF
```

该产品会明确显示不确定性和证据缺口，不把模型输出伪装成无条件事实。
