# 棱镜 PRISM — 开源软件选型调研

> **调研日期**：2026-08-31
> **调研范围**：系统四层架构的开源实现
> **结论摘要**：每一层都有成熟开源方案；不需要自研核心组件，建议在开源框架之上做薄封装。

---

## 1. 爬虫 / 采集层

| 方案 | 语言 | 许可 | 定位 | 是否适合棱镜 |
|---|---|---|---|---|
| **Scrapy** | Python | BSD-3 | 生产级静态/API 大规模采集，内置队列、去重、中间件 | ✅ 首选：政策源多为静态页/RSS，Scrapy 最稳 |
| **Crawlee** | Node/TS | Apache-2.0 | Apify 出品，可切换 Cheerio/Playwright 引擎，内置 RequestQueue | ⭐ 备选：若新闻源需要 JS 渲染 |
| Crawl4AI | Python | Apache-2.0 | LLM 友好的爬虫，输出结构化文本 | 可作正文提取辅助 |
| Playwright | 多语言 | Apache-2.0 | 浏览器自动化，JS 渲染 | 仅处理少数 JS 站点 |
| feedparser | Python | BSD-2 | RSS/Atom 解析 | 棱镜必用（官方政策源 RSS 优先） |

**棱镜建议**：
- 主线：**Scrapy** + feedparser（政策源 RSS + 静态页）。
- 少数需 JS 渲染的新闻站：按需引入 Playwright，或直接用 Crawlee 做混合引擎。
- 正文提取：可选集成 trafilatura（纯文本提取）作为 Scrapy 中间件。
- 独立运行、独立 venv、独立队列——不与任何现有爬虫混用。

---

## 2. 文本证据库 / 索引层

| 方案 | 定位 | 是否适合棱镜 |
|---|---|---|
| **SQLite + FTS5** | 轻量全文检索，单文件，零部署 | ✅ 首选（M0 足够） |
| Meilisearch | 独立搜索服务，中文友好，快 | ⭐ 备选（材料量大后升级） |
| Typesense | 类 Meilisearch，内存索引 | 备选 |
| OpenSearch | 重量级全文+聚合 | 过重，不推荐 |
| Qdrant / Milvus / Weaviate | 向量语义检索 | 仅当需要语义检索时考虑，M0 不引入 |

**棱镜建议**：
- M0：**SQLite + FTS5**（frontmatter 元数据 + 全文索引），零依赖。
- 材料量上升后：升级 **Meilisearch**（中文分词好、部署轻），或按需求加向量检索。
- 不引入重型搜索引擎，保持项目独立、轻量。

### 2.1 PDF 提取与 OCR（摄入层依赖）

| 方案 | 许可 | 定位 | 是否适合棱镜 |
|---|---|---|---|
| **pdfplumber** | **MIT** | PDF 文本 + 表格提取 | ✅ 首选（文本与表格兼顾，MIT 兼容） |
| **RapidOCR** | **Apache-2.0** | 扫描件 OCR（onnxruntime，可 GPU） | ✅ 首选（扫描版政策文件必需） |
| pypdf | BSD-3 | 轻量 PDF 元数据/文本 | 备选 |
| ~~PyMuPDF~~ | ~~AGPL-3.0~~ | ~~提取性能强~~ | ❌ **排除**：AGPL 传染性会破坏 MIT 开源合规 |

> ⚠️ **许可红线**：棱镜以 MIT 开源，所有依赖必须 MIT/Apache/BSD 兼容。PyMuPDF 虽性能强，但 AGPL-3.0 的强 copyleft 会使下游用户承担合规义务，**明确排除**，不作为必选或默认依赖。

---

## 3. 时序图谱层（GTI/Graphiti）

| 方案 | 定位 | 是否适合棱镜 |
|---|---|---|
| **Graphiti（Zep）** | 时间感知知识图谱框架，`valid_at/invalid_at` 双时态边、自动事实失效、episode 级溯源 | ✅ 首选（已是棱镜定稿决策） |
| 底层图库 | Graphiti 支持 **Neo4j / FalkorDB / Amazon Neptune** | Neo4j 最成熟，FalkorDB 更轻 |
| EventKG | 事件知识图谱模型（学术项目） | 可参考建模，非运行框架 |
| Cognee | 多模态知识抽取（与 Graphiti 常被对比） | 偏记忆场景，棱镜不需要 |

**Graphiti 关键事实（2026-08-31 确认）**：
- 开源，MIT 许可，GitHub `getzep/graphiti`，作者团队 Zep。
- 运行在 Neo4j（≥5.26）/ FalkorDB（≥1.1.2）/ Neptune 上，可完全自托管。
- 提供 MCP server 部署方式（Docker + Neo4j）。
- 双时态边、自动事实失效、episode 级溯源——正好对应棱镜"演变+失效+可回溯"需求。
- 棱镜部署：独立 Neo4j（或 FalkorDB）+ 独立 Graphiti 实例 + 独立配置/凭据/备份。

> 与棱镜需求的对齐度极高：Graphiti 本来就是"记录什么现在为真、什么曾为真"的设计，`valid_at/invalid_at` 正是棱镜追踪政策/观点演变需要的语义。

> ⚠️ **底层图库许可说明**：Neo4j Community 为 **GPLv3**，FalkorDB 为 **SSPLv1**。棱镜以**独立客户端进程**连接这些图数据库（通过驱动 API），不包含、不修改、不分发其代码——GPL/SSPL 的传染义务不适用于"独立进程间通信"这一边界。Docker Compose 部署时两者以独立容器运行。此边界需在开源文档（README/贡献指南）中向贡献者明确说明。

---

## 4. 多代理辩论层

| 方案 | 语言 | 定位 | 是否适合棱镜 |
|---|---|---|---|
| **AutoGen / AG2** | Python | 多智能体对话框架，原生支持"多代理辩论"设计模式（agents 多轮互答、互相质询、基于他人回答修正） | ✅ 首选 |
| **LangGraph** | Python | 图结构编排，节点/边/状态机，控制力强 | ⭐ 备选：若要精细控制辩论流程 |
| **CrewAI** | Python | 角色扮演式（role-based DSL），上手快 | 备选：但角色模式可能把立场写死成"人设" |
| OpenClaw | TS/Python | 社区资源多 | 不匹配（偏个人助理） |

**关键发现**：AutoGen 官方文档有专门的 **Multi-Agent Debate 设计模式**——多轮互动、agent 互相交换回答并基于他人回答修正，正是棱镜"多立场辩论"所需的核心原语。

**棱镜建议**：
- **AutoGen/AG2** 为辩论编排框架（agent 即"分析视角"）。
- 强制引文、证据链约束在 agent 的 system prompt 层实现（"观点必须附 source_id"），框架保证对话流转，语义约束由棱镜层注入。
- 自动仲裁：AutoGen/AG2 可编排多个视角自动互相质询、修正和收敛；用户介入以追加材料、指定问题和目标案例为主，不要求逐条仲裁候选。
- 若后续辩论流程复杂（分阶段、条件分支），可迁移 LangGraph 做显式图编排。

---

## 5. 报告 / 交付层

| 方案 | 定位 | 是否适合棱镜 |
|---|---|---|
| **Markdown + md2pdf** | 报告结构化文本，转 PDF | ✅ 首选（已有成熟流程） |
| Jinja2 模板 | 报告模板渲染 | ✅ 配合使用 |
| Quarto / Pandoc | 学术级文档转换 | 备选（报告要更规范时） |

**棱镜建议**：Markdown 报告 + Jinja2 模板 + 现有 md2pdf 流程，保持轻量。

---

## 6. 综合选型矩阵

| 层 | 首选开源方案 | 理由 |
|---|---|---|
| 爬虫 | **Scrapy**（+ feedparser，按需 Playwright） | 成熟、静态页/RSS 最优 |
| 文本库 | **SQLite + FTS5**（量大后 Meilisearch） | 零依赖起步，可升级 |
| 时序图谱 | **Graphiti + Neo4j/FalkorDB** | 时间感知、事实失效、可溯源，正中需求 |
| 多代理辩论 | **AutoGen/AG2**（备选 LangGraph） | 原生多代理辩论 + 人类介入 |
| 报告 | Markdown + md2pdf | 轻量、已验证 |

---

## 7. 与棱镜"独立项目"约束的核对

- 所有方案均**可完全自托管**：Scrapy/SQLite/Graphiti/AutoGen 全部本地部署，无外部依赖。
- 全部**开源免费**（MIT/BSD/Apache-2.0），无授权锁定。
- 不依赖任何私人记忆系统或外部 Agent 框架，纯自托管，符合独立项目边界要求。
- 各层可独立替换：爬虫换 Crawlee、图谱换 FalkorDB、辩论换 LangGraph，互不影响。

---

## 8. 建议的下一步

1. M0 技术栈定型：**Scrapy + SQLite/FTS5 +（图谱暂不接入，先跑单案例时间线）**。
2. M1 引入 **Graphiti + 独立 Neo4j**，验证"政策演变时间线 + 事实失效"。
3. M2 引入 **AutoGen** 辩论编排，接入任意 OpenAI 兼容 LLM（如火山方舟、硅基流动或其他）。
4. 每层先做最小验证（spike），确认后再定型，避免一上来全栈。

---

*本文档为棱镜 PRISM 开源选型调研。独立项目，不依赖任何私人系统。*
