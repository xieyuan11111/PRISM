# PRISM v1 范围冻结

**版本：** v1 初步产品
**定位：** 本机自托管、单用户、loopback-only 的政策与学术观点演变追踪工具

## 1. v1 目标

PRISM v1 必须让用户能够通过本机 WebUI 完成一条可审计的基本工作流：

```text
启动服务
→ 选择或查看案例
→ 查看历史时间线与状态
→ 检索证据
→ 追加材料
→ 运行 LLM 抽取
→ 查看 pipeline / mechanism / semantic / evidence gap
→ 发起多视角辩论
→ 追问视角
→ 保存报告并导出 PDF
```

v1 是初步产品，不是完整商业化平台，也不是自动替用户作出政治、法律、投资或学术最终裁决的系统。

## 2. 纳入 v1

| 模块 | v1 范围 | 状态要求 |
|---|---|---|
| 材料摄入 | Markdown、PDF、OCR、raw 留存、corpus Markdown 正本 | implemented |
| 索引 | SQLite / FTS5、检索与过滤 | implemented |
| LLM 抽取 | 官方 OpenAI Python SDK、严格证据/时间/source/case 校验 | implemented；真实语义可能 semantic-partial |
| 案例 | 单案例完整使用；底层保留 case_id 隔离 | implemented |
| 时序 | timeline、snapshot、cutoff、compare | implemented |
| 图谱 | 可选 Graphiti / 原生 Neo4j 时序后端 | live-mechanism-pass；真实语义单独判定 |
| WebUI | `/`、`/debate`、`/evidence`、`/materials` | 初步产品必须完成本机真实冒烟 |
| 辩论 | 多视角解释、交叉质询、综合、指定视角追问 | implemented |
| 报告 | Markdown 版本账本与 PDF 导出 | implemented |
| LLM transport | 官方 `openai` SDK；默认离线、lazy import | implemented |
| 实验能力 | prompt profiles、benchmark、protocol-v2、split-v1 | experimental，不作为默认生产路径 |
| 安全 | loopback-only、密钥不入库、不展示明文、不泄露路径 | 必须通过发布前扫描 |
| 安装 | 从零安装、配置、启动、使用说明 | 必须完成 |

## 3. v1 默认行为

```text
默认抽取路径：baseline
默认图谱后端：OfflineGraphBackend，除非显式启用 Graphiti
默认安装：核心 dependencies 为空
默认网络：不主动连接外部服务
默认 WebUI：127.0.0.1，禁止自动打开浏览器
```

实验 profile：

```text
protocol-v1：experimental
protocol-v2：experimental
split-v1：experimental
```

实验 profile 必须显式启用，并使用与 baseline 相同的确定性证据、时间、source、case 和 relation 校验；不得用增加节点数量替代语义质量。

## 4. v1 明确不包含

以下功能不阻塞 v1，延后至 v1.1 或更后：

```text
完整多案例交互工作台
实时流式辩论界面
暂停 / 继续 / 重启辩论
拖拽上传
WebUI 模型设置页
多用户认证
公网或局域网远程访问
持续爬虫平台
Graphiti 正式分页
大规模性能优化
```

## 5. 部署边界

PRISM 当前路线不使用 Docker：

```text
Docker 不属于 v1 路线
Docker Compose 不属于 v1 路线
不作为安装前置
不作为 CI 前置
不作为 Graphiti 验收前置
```

Graphiti 如启用，使用：

```text
原生 Neo4j launcher
独立 Neo4j home
独立 data / logs / run / import
loopback 监听
专用端口
```

## 6. v1 通过定义

v1 可以发布为初步产品，必须同时满足：

```text
用户可按文档启动 WebUI
WebUI 本机真实冒烟通过
核心离线测试通过
官方 OpenAI SDK 路径通过
真实 Graphiti 机制链有明确验收记录
真实 semantic-partial 未被包装成 pass
实验 profile 的边界和失败语义清楚
治理文件、CI、lint、依赖和安全扫描完成
```

以下不是 v1 的硬性通过条件：

```text
所有真实案例都 semantic-pass
所有案例都产生 relation
完整多案例 UI
持续爬虫平台
Graphiti 大规模分页
实时辩论控制
远程认证
Docker 部署
```
