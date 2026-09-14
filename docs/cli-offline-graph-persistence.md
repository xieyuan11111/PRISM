# CLI 默认离线图谱持久化修复说明

> **日期**：2026-09-14
> **触发**：真实运行 HN-AD 综述/全文材料时发现：案例账本已保存候选，但新 CLI 进程的 `timeline`/`report` 读取空时间线。

## 问题

默认 `OfflineGraphBackend` 是 `dict[str, GraphEpisode]`：仅在单个 Python 进程内存活。

```text
prism process material.md
  → graph write 成功（本进程内）
  → 命令退出，dict 丢失

prism timeline CASE
  → 新 runtime、新 dict
  → 空时间线

prism report CASE
  → 空报告（empty_timeline）
```

这使 CLI-first 的跨命令工作流不可用，即便 `case_extraction_ledger` 已持久化候选。

## 目标

在**未启用 Graphiti/Neo4j**时，默认离线图后端必须跨 CLI 进程持久化 `GraphEpisode`，使以下工作流成立：

```text
prism process
→ 新进程 prism timeline
→ 新进程 prism state/snapshot/compare
→ 新进程 prism report/rebuild-report/report-pdf
```

## 约束

1. 不引入第二套事实/时间语义库；持久化的是现有 `GraphEpisode`，它本就是 GraphService 的输入契约；
2. 不影响显式 Graphiti 配置：Graphiti 继续走独立 registry 和真实后端；
3. 不把 OfflineGraphBackend 冒充真实 Graphiti；返回/文档仍应能区分 backend=`offline`；
4. 幂等：相同 `episode_key` 重复写入必须跳过；
5. 必须保存/读回 `episode_key/name/case_id/kind/episode_body/reference_time/valid_at/invalid_at/source_ids/confidence/provenance/evidence/evidence_role/cited_source_ref`；
6. 所有 datetime timezone-aware；坏行 fail-closed（忽略坏 episode 并保留可审计警告，而不猜数据）；
7. 旧 `PRISM_HOME` additive migration，无需手工迁库；
8. WebUI 不新增功能；修复的是 core runtime/CLI 可靠性。

## 推荐实现

复用 `SQLiteEpisodeRegistry`：它已经是项目自有 SQLite、可完整序列化/反序列化 `GraphEpisode` 的成熟组件。

新增 `SQLiteOfflineGraphBackend`：

```python
class SQLiteOfflineGraphBackend:
    def __init__(self, registry: SQLiteEpisodeRegistry): ...
    async def add_episode(self, episode: GraphEpisode) -> bool:
        # registry.get(key, group_id="offline") → offline 组内已存在则 False
        #   （group-scoped：foreign Graphiti 组的同 key 行不拦截 offline 写入，反之亦然）
        # registry.put(episode, group_id="offline") → True
    async def search(self, query: str) -> tuple[GraphEpisode, ...]:
        # registry.list_episodes(group_id="offline")
```

`SQLiteEpisodeRegistry` 增加只读 `list_episodes(group_id: str)`，按 `case_id/valid_at/episode_key` 稳定排序。表主键为 `(episode_key, group_id)`：同一确定性 `episode_key` 可在 offline 组与 Graphiti 组各自独立存在，`get(episode_key, group_id=...)` 按 `episode_key AND group_id` 过滤。隔离标识：

```text
database = "offline"
group_id = "offline"
```

`create_runtime()`：当 `graphiti.enabled == false` 且调用者没有显式注入 graph backend 时，创建 `SQLiteEpisodeRegistry(paths, database="offline")` + `SQLiteOfflineGraphBackend`；runtime 关闭时关闭 registry。

保留旧 `OfflineGraphBackend` 仅供单元测试/明确注入；不要把它作为默认 runtime 后端。

## 验收

1. 临时 `PRISM_HOME` 中，runtime A 写 episode 后关闭；runtime B 的 `timeline/state/report` 能读回；
2. 相同 episode 写两次，第二次 `added_keys=[]`、`skipped_keys=[key]`；
3. `group_id/database=offline` 不读取或污染 Graphiti 的记录；
4. Graphiti 配置路径仍用 GraphitiBackend，不走 offline SQLite backend；
5. 真实 HN-AD 案例：运行 `prism merge-case stress-tolerant-hnad-strains-2026` 后，新进程 `prism timeline ...` 不再为空；
6. 真实 `prism report ... --no-llm` 不再产生 `empty_timeline`；
7. 既有 graph/runtime/CLI/WebUI 测试全绿，ruff/compile/diff check 通过。
