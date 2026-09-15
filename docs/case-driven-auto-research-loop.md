# PRISM 案例驱动自动研究循环说明

> **定位**：产品与技术设计说明，不是本次实现提交。
> **日期**：2026-09-15
> **当前状态**：自动研究循环尚未接入生产代码；本文用于明确目标行为、职责边界和停止条件。
> **相关文档**：`REQUIREMENTS.md`、`docs/cli-ai-callable-requirements.md`、`docs/cli-ai-callable-design.md`、`docs/cli-offline-graph-persistence.md`

---

## 1. 这套机制到底要做什么

PRISM 的自动抓取不是“用户给一个 URL，程序抓一次”，而应该是**以案例为中心的自动研究循环**：

```text
用户指定一个案例，并提供初始材料
        ↓
LLM 阅读当前材料与案例状态，提取概念、实体、阶段、机制和证据缺口
        ↓
根据概念生成搜索方向、时间窗口、来源类型和查询词
        ↓
在已配置的公开来源白名单中自动搜索
        ↓
搜索结果只作为 discovery lead，不直接作为证据
        ↓
对候选 URL 重新抓取公开原文
        ↓
原文进入 raw → 标准 Markdown → SQLite/FTS5 → 抽取管线
        ↓
LLM 从原文抽取节点、事实、主张和关系
        ↓
确定性代码校验 source / quote / time / case / evidence
        ↓
只有 graph-ready 候选才写入时序图谱
        ↓
重新读取案例状态，计算本轮新增内容
        ↓
有必要则生成下一轮研究计划；达到停止条件则结束
        ↓
生成或更新新的不可变报告版本
```

因此，用户不需要逐条寻找新闻链接，也不需要逐条接受候选。用户只需要提供：

```text
case_id
初始材料或已有案例
允许访问的来源范围
时间边界
预算与停止条件
```

LLM 负责“概念化和决定下一步看什么”；确定性代码负责“能不能看、能不能信、能不能入图以及什么时候必须停”。

---

## 2. 当前代码已经有什么

当前仓库已经具备自动循环所需的若干底层零件，但它们目前还是一次性调用，不是案例驱动的持续循环。

### 2.1 计划器：`ResearchPlanner`

位置：`src/prism/research/planner.py`

它可以把材料和已有抽取结果整理为 `ResearchPlan`，计划包含：

- 研究时间窗口；
- 研究阶段（proposal、publication、implementation、revision、current 等）；
- 概念（`ResearchConcept`）；
- 允许的来源候选和来源类型；
- 面向来源白名单的搜索查询；
- LLM `source_selector` 失败时的确定性 fallback。

它本身不联网，也不抓取材料。

### 2.2 执行器：`ResearchExecutor`

位置：`src/prism/research/executor.py`

它可以：

1. 把查询交给 `SearchProvider`；
2. 只读取搜索结果中的公开链接；
3. 对候选域名执行白名单过滤；
4. 对规范化 URL 去重；
5. 通过 `SourceIntake.fetch_source()` 重新抓取原文；
6. 将每个候选记录为成功或分类失败。

搜索结果中的标题、摘要和排名不是最终证据。只有重新抓取并经过 PRISM 摄入管线的正文，才有资格成为材料。

### 2.3 来源摄入：`PrismAPI.fetch_source/fetch_sources`

位置：`src/prism/api/facade.py`

它负责把公开 URL 送入已有的：

```text
来源访问校验
→ raw 留存
→ Markdown 标准化
→ SQLite/FTS5 索引
→ 可选 pipeline
→ 事件和审计
```

来源层已经包含 RSS/Atom 解析、公开网页抓取、重定向后域名复核、SSRF 防护和失败分类。

### 2.4 抽取与图谱

已有 `ExtractionService`、`PipelineService`、`CaseService`、`GraphService`，以及默认离线 SQLite 图谱后端：

```text
LLM 抽取
→ 原文 quote 定位
→ 时间和案例校验
→ 自动仲裁
→ case ledger 累计
→ graph-ready episode
→ Offline/Graphiti 图谱写入
```

`docs/cli-offline-graph-persistence.md` 规定的离线图谱持久化已经解决了跨 CLI 进程读回问题。自动研究循环必须复用这条链，不能绕开它自行写事实。

---

## 3. 自动研究循环的输入

一次循环必须以显式目标案例为中心，不能只接收一个模糊主题。

```json
{
  "case_id": "stress-tolerant-hnad-strains-2026",
  "seed_material_ids": ["mat_..."],
  "as_of": "2026-09-15T00:00:00+00:00",
  "source_domains": [
    "pubmed.ncbi.nlm.nih.gov",
    "europepmc.org",
    "sciencedirect.com"
  ],
  "limits": {
    "max_rounds": 3,
    "max_candidates": 30,
    "max_materials": 20,
    "max_llm_calls": 20,
    "max_download_bytes": 524288000,
    "max_wall_time_seconds": 900,
    "stable_empty_rounds": 2
  }
}
```

输入约束：

- `case_id` 必须是调用方明确声明的案例；
- 研究材料可以是初始 PDF、Markdown 或已有案例材料；
- `as_of` 必须带时区；
- 来源域名必须来自项目配置的白名单，LLM 不能自行扩大范围；
- LLM 可以提出概念、同义词和查询词，但不能改变 `case_id`、案例类型、案例名称或时间边界。

---

## 4. LLM 在循环中的职责

### 4.1 概念化

LLM 读取当前案例的：

```text
已接受的节点
已接受的时间事实
已接受的主张
已有材料标题/摘要/元数据
证据缺口
尚未覆盖的研究阶段
```

然后提出结构化研究对象：

```text
概念：HN-AD、Acinetobacter、Pannonibacter sp. W30
别名：菌株名、旧称、缩写
主体：菌株、研究团队、反应器、污染物
机制：同化、膜转运、氮代谢基因、氧限制
条件：高氨、低温、高盐、重金属、C/N 比
阶段：发现、鉴定、机制验证、反应器应用、规模化
缺口：缺少全文、缺少重复验证、缺少长期工程数据
```

这些内容是**研究计划输入或待验证假设**，不是事实。它们只有在候选原文被重新抓取、引用被逐字定位并通过校验后，才可进入图谱。

### 4.2 搜索计划

LLM 可以为每个概念提出：

- 查询词；
- 时间窗口；
- 来源类型（论文、官方文件、新闻、数据）；
- 白名单内的来源域名；
- 查询理由；
- 预期寻找的研究阶段或证据缺口。

LLM 不可以：

- 直接把搜索摘要写入事实；
- 发明不在白名单中的 URL；
- 把“可能存在”写成“已经存在”；
- 以自己的判断决定整项研究何时结束。

### 4.3 研究方向的递进

下一轮计划应基于**已经入图的新增知识**和**未解决的证据缺口**生成，而不是无条件重复上一轮查询。

```text
第 1 轮：从综述提取菌株、胁迫条件和机制概念
第 2 轮：寻找具体菌株的原始实验论文
第 3 轮：寻找机制验证、反应器应用和长期稳定性研究
```

每轮计划都必须记录 fingerprint。重复 fingerprint 视为循环风险，不得无限执行。

---

## 5. 从搜索结果到图谱的确定性链路

```text
SearchProvider 返回 lead
        ↓
检查是否存在公开 link
        ↓
规范化 URL、检查白名单和查询域名
        ↓
SourceIntake 重新抓取原文
        ↓
校验 HTTP 状态、正文存在性、访问边界和重定向
        ↓
raw 留底 + Markdown 标准化 + material hash
        ↓
SQLite/FTS5 索引和材料去重
        ↓
ExtractionService 从材料正文抽取候选
        ↓
quote 必须是正文单段连续原文
        ↓
校验 source_id、时间、case_id、枚举和引用完整性
        ↓
自动仲裁候选；失败保留为 gap/audit
        ↓
CaseService 累计同一案例
        ↓
GraphService 写入 GraphEpisode
```

以下情况不能进入图谱：

```text
搜索摘要没有原文链接
候选链接不在白名单
正文为空或只是登录/付费墙页面
quote 无法在正文逐字定位
quote 跨越多个段落
时间不明且无法保守表达
案例绑定漂移
只有论文发表事件，没有实质演变内容
LLM 推理没有证据支持
```

“抓取成功”只表示材料被合法获取并进入摄入流程；不表示其中每个模型候选都能成为事实。

---

## 6. 停止条件：由代码决定，不由 LLM 决定

停止条件分为**收敛停止、预算停止、边界停止、失败停止和用户停止**。

### 6.1 收敛停止

#### A. 没有新的可执行查询

```text
计划为空；或
所有查询已执行；或
所有查询引用的来源域名都不可用；或
查询只会重复已处理的 URL。
```

停止原因：

```text
no_new_executable_queries
```

#### B. 连续没有研究增量

一轮结束后计算 delta：

```text
new_material_ids
new_episode_keys
new_case_nodes
new_temporal_facts
new_claims
```

当连续 `stable_empty_rounds` 轮都没有新增有效材料或 graph-ready episode 时停止。

停止原因：

```text
no_progress
```

单轮搜索返回空不一定立即停止，避免暂时性网络空结果导致过早结束。

#### C. 已达到证据前沿

如果所有尚未解决的方向都只能得到：

```text
重复材料
无正文页面
摘要级 metadata-only 材料
无法定位的候选
```

则停止并记录：

```text
evidence_exhausted
```

这表示当前允许来源和证据条件已经耗尽，不表示案例完整。

#### D. 计划循环

如果新一轮的 plan fingerprint 已经出现过，停止：

```text
cycle_detected
```

不得通过改写查询词的表面文字绕过循环检测。

### 6.2 预算停止

任何一个硬预算达到上限都必须停止：

```text
max_rounds
max_candidates
max_materials
max_llm_calls
max_download_bytes
max_wall_time
```

预算停止的运行状态是：

```text
status = "stopped"
```

不是 `success`，也不是 `converged`。返回结果中必须同时给出：

```text
已执行轮数
已经抓取的 URL 数
新增材料数
新增 episode 数
未处理候选数
剩余证据缺口
stop_reason
```

### 6.3 安全和基础设施停止

以下情况应终止当前任务，不能继续猜测：

```text
白名单或 SSRF 检查失败达到任务级阈值
重定向进入禁止域名
持久化写入失败
图谱写入状态不确定
研究计划无法通过 schema 校验
发现跨案例绑定漂移
检测到数据损坏
```

停止原因示例：

```text
security_rejected
persistence_failed
plan_invalid
case_binding_failed
cycle_detected
```

单个候选失败只记录为 `CandidateFailure` 并继续；整个计划的 planner/executor/monitor 失败才升级为任务级 `failed`。

### 6.4 用户停止和进程停止

```text
用户显式取消
进程收到 Ctrl+C / termination
达到 watch 模式的最大运行时间
```

状态：

```text
status = "cancelled"
stop_reason = "cancelled"
```

取消时不删除已经合法摄入的 raw、corpus 或已提交图谱事实。

---

## 7. 单轮与持续监测要分开

### 7.1 单轮自动研究

未来 CLI 形态建议：

```bash
prism research-case CASE_ID --source-id MATERIAL_ID \
  --max-rounds 3 \
  --max-candidates 30 \
  --max-wall-time-seconds 900
```

命令执行：

```text
开始 → 运行若干研究轮次 → 达到停止条件 → 输出 ResearchRun JSON → 退出
```

### 7.2 持续监测

持续监测是单轮循环外面的一层 watcher：

```text
等待 interval
→ 为案例建立当前 frontier
→ 执行一轮自动研究
→ 保存运行结果
→ 等待下一次 interval
```

未来可采用：

```bash
prism research-case CASE_ID --watch --interval 1h
```

watch 模式自身的停止条件：

- 用户显式停止；
- 达到 watcher 最大运行时长；
- 每日 URL/LLM/下载预算耗尽；
- 连续多次 provider 失败触发熔断；
- 来源被禁用或白名单发生变化；
- 进程退出。

“本轮收敛”不等于“持续监测服务停止”。

---

## 8. 运行结果契约

建议结果使用不可变、带时区的 DTO，并通过 CLI 输出 JSON：

```json
{
  "run_id": "research-run-...",
  "case_id": "stress-tolerant-hnad-strains-2026",
  "status": "converged|stopped|failed|cancelled",
  "stop_reason": "no_progress",
  "started_at": "2026-09-15T05:00:00+00:00",
  "finished_at": "2026-09-15T05:08:12+00:00",
  "round_count": 2,
  "new_material_ids": ["mat_..."],
  "new_episode_keys": ["episode-..."],
  "failed_candidates": [
    {
      "url": "https://example.org/paper",
      "kind": "no_content",
      "detail": "authoritative intake returned no collectible body"
    }
  ],
  "rounds": [
    {
      "round": 1,
      "plan_fingerprint": "sha256:...",
      "queries": 4,
      "candidates_attempted": 8,
      "new_material_count": 1,
      "new_episode_count": 6,
      "progressed": true
    }
  ],
  "evidence_gaps": [
    {
      "gap_type": "full_text_unavailable",
      "detail": "abstract-only sources remain unverified as substantive evidence"
    }
  ],
  "budget": {
    "rounds_used": 2,
    "candidates_used": 8,
    "llm_calls_used": 5,
    "download_bytes_used": 37140
  },
  "report_version_id": "rv_..."
}
```

其中：

- `new_material_ids` 只来自真实摄入报告；
- `new_episode_keys` 只来自真实图谱写入或可验证 graph-ready 结果；
- `failed_candidates` 不能被隐藏；
- `report_version_id` 没有生成时必须是 `null`；
- `status="stopped"` 不代表语义质量通过；
- 任何未知字段使用 `null` 或 `unknown`，不借用其他阶段的值。

---

## 9. HN-AD 综述的目标运行示例

对“耐胁迫 HN-AD 菌株”案例，理想自动流程是：

```text
初始材料：2026 年综述
        ↓
概念化：Acinetobacter / Pseudomonas / W30 / HA2 / ND7 / TAC-1
        ↓
概念化：低温 / 高氨 / 高盐 / 重金属 / 膜转运 / 氮代谢基因
        ↓
生成查询：原始研究、机制验证、反应器应用、固定化与共培养
        ↓
白名单内搜索候选论文
        ↓
重新抓取可访问全文
        ↓
将正文送入抽取和证据校验
        ↓
W30 全文形成 4 节点、6 事实、4 主张
        ↓
写入离线持久图谱
        ↓
下一轮只寻找尚未覆盖的机制/工程证据
        ↓
达到 no_progress 或 evidence_exhausted 后停止
        ↓
生成报告，明确哪些是原始观察、哪些是综述转述、哪些仍是缺口
```

HA2、ND7、TAC-1 只有摘要时，应保留为：

```text
metadata_only / abstract_only
```

不能因为它们的标题和摘要看起来相关，就自动把摘要中的数字提升为与全文相同等级的图谱事实。

---

## 10. 绝对不能做的事情

```text
不能让 LLM 自己决定无限继续抓取
不能把搜索结果摘要直接写入图谱
不能把相关标题当成原始证据
不能因为找到很多重复新闻就报告“研究完成”
不能把预算耗尽写成成功
不能把 metadata-only 当成 substantive evolution
不能按标题/标签/embedding 猜 case_id
不能绕过登录墙、付费墙、验证码或反爬
不能用 UI 或编排器自己创建第二套事实库
不能覆盖旧报告版本
不能用“抓取成功”代替“证据验证成功”
```

---

## 11. 当前实现结论

截至本文日期，PRISM 已有：

```text
一次性计划生成
一次性搜索与重新抓取
原文摄入、标准化、索引
LLM 抽取和证据门禁
案例累计
离线/Graphiti 图谱写入
跨 CLI 进程的离线图谱持久化
```

尚未接入生产代码的部分是：

```text
以 case_id 为中心的多轮自动研究编排器
确定性停止策略的运行实现
ResearchRun 持久化/查询
research-case CLI 入口
watch 定时监测模式
```

因此本文描述的是**应当实现的自动研究机制和停止条件**，不是把尚未完成的自动抓取能力提前宣称为已经存在。

最终目标可以概括为：

> **让 LLM 负责发现“还应该看什么”，让 PRISM 负责验证“看到了什么、能不能相信、是否应该入图，以及什么时候必须停”。**

---

## 12. 实测记录（2026-09-15，HN-AD 综述案例）

本节记录按上述流程对“耐胁迫 HN-AD 菌株”案例做真实执行时观察到的事实，用于校正设计与后续实现。所有条目均来自真实命令输出，未做推测。

### 12.1 planner 概念化输出会被截断，并退回无意义的 fallback

```text
现象：prism discover <综述材料>
结果：origin=fallback
warning：source_selector output rejected: completion is not valid JSON:
        Unterminated string starting at: line 913 column 9 (char 31969)
```

原因：提示词要求“提取材料中**每一个**可搜索概念，并为每个概念生成查询”，而种子材料是约 80KB 的整篇综述，输出 JSON 必然超出模型输出上限而截断。

后果：fallback 计划只产出 1 个概念（论文标题本身）与政策向模板查询（`proposal draft` / `implementation rollout` / `revision amendment update`），对学术案例无检索价值。

改进方向：

```text
- 概念数量必须受上界约束（MAX_RESEARCH_CONCEPTS / CONCEPT_TARGET_* 已有常量）
- 交给 planner 的材料应有界（题名 + 摘要 + 结构化抽取结果），而不是整篇正文
- 输出被截断时应记录为“计划失败但可重试”，而不是静默当作正常 fallback
```

### 12.2 通用查询精度极低，字段级查询才可用

```text
模糊自然语言概念 → Europe PMC 全文检索：返回珊瑚微生物、土壤、水产等无关综述
（相关命中率接近 0）

改为 TITLE_ABS: 字段精确查询后：
  低温、耐盐、固定化、群体感应、基因组等方向均返回高度相关原始研究
```

改进方向：概念到查询的生成必须产出**字段级、术语级**查询（如 `TITLE_ABS:"heterotrophic nitrification" AND TITLE_ABS:"low temperature"`），并在计划中记录每个概念的命中质量。

### 12.3 HTML 页面抓取不适合作学术证据来源

```text
prism fetch https://pmc.ncbi.nlm.nih.gov/articles/PMC5331289/ --kind page
→ 抓到约 37KB 文本，但被站点导航污染：
   "Skip to main content" / "Official websites use .gov" / "Search NCBI" ...
→ 有效长段落仅 1 段
→ published_at 被写成抓取时间（2017 年论文记为 2026-09-15）
→ type 误判为 news
```

改进方向：

```text
- 学术来源优先使用 Europe PMC fullTextXML（JATS 元数据，题名/作者/日期准确）
- 页面提取器需要正文区优先抽取与元数据日期解析，否则时间语义会被污染
```

### 12.4 prism fetch 摄入成功却返回失败

```text
材料已成功入库（mat_1b0a29ba7220ee01ced68da5，corpus 已生成）
但自动管线订阅者报错：
  LookupError: material not found: mat_1b0a29ba7220ee01ced68da5
→ CLI 以退出码 1 结束
```

这是真实的运行时缺陷：源摄入路径发布 `material.ingested` 后，自动管线无法解析该材料。需要修复 resolver 的材料查找（与索引写入时序相关）。

### 12.5 候选级引文缺失是真实失败模式，门禁行为正确

```text
PMC9488085（群体感应调控 HN-AD）
→ 27 个节点候选全部被拒：
   nodes[i].evidence must not be empty
   LLM 自动仲裁：Gapped candidate lacks required evidence;
                no safe revision possible without inventing quote/source/time.
```

模型确实有机会产出候选，但未附引文；确定性门禁与仲裁器拒绝凭空补造。**这是期望行为，不是故障。** 可考虑的改进是候选级“仅补引文”重试，仍拿不到就保持拒绝。

### 12.6 摘要级材料不会被当成证据

HA2 / ND7 / TAC-1 三份仅摘要材料被正确标记 `metadata_only`，产出 0 候选、未进入图谱；只有可读取全文的原始研究才产出候选。该边界在真实运行中成立。

### 12.7 跨进程图谱持久化在真实案例上有效

```text
prism merge-case stress-tolerant-hnad-strains-2026
→ 10 份材料、23 节点、23 事实、13 主张、1 冲突
→ 写入 added_keys=0 / skipped_keys=71（幂等）

新进程 prism timeline
→ 69 条 episode（1 evolution_case、10 material_provenance、
  23 evolution_node、21 temporal_fact、13 claim、1 temporal_relation）
→ 覆盖 2017–2024 的不同胁迫方向
```

### 12.8 中文报告链路当前不可交付

```text
core 分支：20 个测试失败
  既有英文断言与新默认 zh-CN 冲突
  新实现自身也有 bug（legacy 库回读、prompt 语言、CLI 转发）
PDF 侧：章节名校验（Executive Summary / Timeline Stages / Citations）
        尚未本地化，中文报告无法通过 PDF 回读验证
```

结论与建议：

```text
- 默认报告语言保持 en（不破坏既有行为与测试）
- 中文以 --lang zh-CN 显式选择
- 中文 PDF 必须等 PDF 侧章节名与元数据本地化完成后再开放
```

### 12.9 本轮流程的实际形态

由于多轮自动循环尚未实现，本轮是**按文档流程手工执行**：

```text
我（作为 LLM 角色）完成概念化与候选发现
→ 用 Europe PMC 检索 API 取得字段级候选
→ 用 fullTextXML 取得干净正文并转成 PRISM 标准 Markdown
→ 由 PRISM 完成权威摄入、LLM 抽取、证据校验、案例累计、图谱写入
→ 新进程读回时间线
→ 生成不可变报告版本与 PDF
```

这条路径验证了文档第 5 章“从搜索结果到图谱的确定性链路”是可用的；也说明真正缺失的是**把上述手工步骤编排成受预算与停止条件约束的自动循环**。
