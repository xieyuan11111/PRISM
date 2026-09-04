# 真实案例质量 Gate

`tools/prism_quality_gate.py` 是一个 stdlib-only 的离线质量指标工具，
用于检查 PRISM 项目外的 Release Candidate 运行产物。它读取
`run-summary.json` 和项目自己的 `index.db`，不调用 LLM、不联网、不写入
输入数据库，也不读取项目外的记忆或服务。

## 使用

```console
python tools/prism_quality_gate.py \
  --run-dir /path/to/prism-run \
  --case-id CASE_ID \
  --output /path/to/quality.json \
  --indent
```

也可以显式传入：

```console
python tools/prism_quality_gate.py \
  --run-summary /path/to/run-summary.json \
  --index-db /path/to/index.db \
  --case-id CASE_ID
```

## 指标与判定

工具输出材料成功/失败、案例账本、实质节点/事实/观点/关系、类型和
证据角色分布、`source_ids` 覆盖率、`EvidenceLocator` 覆盖率、证据缺口、
失效/修订/冲突关系以及待绑定材料数量。

判定分为：

- `mechanism_status`：材料是否能被处理、账本和结构是否可读；
- `semantic_status`：是否有实质候选、零缺口、完整引用/定位覆盖、引用来源
  可解析且不存在待绑定材料。

pipeline 全部成功并不等于 semantic pass。存在失败材料、证据缺口、引用
不完整或实质候选为空时，结果会保守地保持 `partial` 或 `fail`。

`--strict` 会在任一 verdict 不是 `pass` 时返回退出码 1；普通模式返回
退出码 0，但 verdict 会如实保留在 JSON 中。输入/数据/脱敏错误分别返回
明确的非零退出码。

## 隐私边界

输出只包含计数、比率、闭合词表标签和判定原因，不复制材料正文、URL、绝对
路径、密钥或凭据。输入运行目录可以位于项目外；真实案例验收记录也应保留
在项目外，不进入 Git。
