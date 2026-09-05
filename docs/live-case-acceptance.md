# 真实 Graphiti 窄案例验收 Runner

## 当前状态

`tools/run_live_case_acceptance.py` 是 PRISM Release Candidate 的真实后端验收 runner。它不会自行启动或停止 Neo4j/Graphiti，不把 `OfflineGraphBackend` 当作真实后端，输入材料与输出摘要均通过调用方参数放在项目外。

2026-09-05 已使用 PRISM-owned 原生 Neo4j Community 5.26、真实 provider 和窄北京政策材料完成一次端到端机制验收：

```text
真实 provider → 真实材料 → pipeline → Graphiti write
→ restart/readback → historical cutoff → report version → PDF
```

结果：

```text
mechanism_status = pass
semantic_status = partial
```

## 验收内容

runner 检查：

- 材料自动 pipeline、目标案例绑定和 graph write；
- 真实 Graphiti 的 case/node/fact/claim/relation readback；
- source/evidence 与 valid/invalid/reference 时间字段；
- fresh runtime 重启后的 registry/readback；
- 两个历史 cutoff 与 future-leak；
- 报告版本与 PDF；
- 脱敏 JSON/Markdown 验收摘要。

公开摘要只保留计数、状态、类型和安全 ID，不包含材料正文、quote、candidate payload、密钥或绝对路径。

## 当前边界

真实机制链已经通过，但真实 LLM 语义质量仍为 `partial`。已观察到的剩余问题包括：

```text
candidate-level validation gaps
quote / paragraph 定位失败
provider 输出波动
完整 proposal → publication → implementation → revision → expiry 链未完成
```

这些不能通过放宽确定性校验或人工制造节点/关系解决。

## 运行前置

```text
PRISM-owned 原生 Neo4j 已启动
HTTP 与 Bolt 只监听 loopback
PRISM_GRAPHITI_PASSWORD 由当前进程环境提供
真实 provider 的 API key 由当前进程环境提供
```

runner 缺少前置条件时返回 `BLOCKED`，不会伪造 `PASS`。它只连接调用方指定的项目自有 Graphiti 实例，不接触其他数据库。

## 部署路线

PRISM 不使用 Docker 或 Docker Compose 作为安装、运行、CI 或验收前置。Graphiti 采用：

```text
原生 Neo4j launcher
独立 Neo4j home
独立 data / logs / run / import
loopback 监听
专用端口
```
