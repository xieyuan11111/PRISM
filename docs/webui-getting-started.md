# PRISM WebUI 初步产品使用指南

## 1. 产品范围

PRISM v1 是本机单用户、loopback-only 的初步产品。WebUI 提供：

```text
案例列表与筛选
历史时间线、snapshot、cutoff 与 compare
节点、事实、关系和 evidence locator 查看
证据检索
本地 Markdown/PDF 材料追加
多视角辩论与指定视角追问
报告版本与 PDF 导出
```

WebUI 默认只绑定 `127.0.0.1`，不会自动打开浏览器，也不会默认提供远程访问或认证。

## 2. 环境要求

- Python 3.11 或更高版本；
- 要使用 WebUI，安装 `webui` extra；
- 要调用 LLM，安装 `openai-sdk` extra 并配置 provider；
- 要导出 PDF，安装 `pdf` extra，并确保系统有 Edge 或兼容 Chromium；
- 要使用时序 Graphiti 后端，另行安装 `graphiti` extra 并准备项目自有原生 Neo4j。

核心安装不会自动安装上述可选依赖，也不会自动联网。

## 3. 安装

在 PRISM 仓库根目录执行：

```console
python -m pip install -e ".[webui,openai-sdk,pdf]"
```

如果只使用离线 WebUI 与已有本地数据，可以不安装 `openai-sdk`；如果不导出 PDF，可以省略 `pdf`。

## 4. 配置本地数据目录

使用 `PRISM_HOME` 指定 PRISM 的本地数据、配置、corpus、索引和报告目录。示例：

```console
set PRISM_HOME=%LOCALAPPDATA%\prism-home
```

也可以在 Unix-like shell 中使用：

```bash
export PRISM_HOME="$HOME/prism-home"
```

不要把真实 API key、密码或 Cookie 写入 Git、corpus、图谱、日志或报告。

## 5. 配置 LLM provider

PRISM 的自有 LLM 调用统一通过官方 `openai` Python SDK。配置文件只保存 provider 的连接地址、模型名和凭据环境变量名，不保存凭据值。

一个最小的 `PRISM_HOME/config.json` 结构如下：

```json
{
  "llm": {
    "providers": {
      "example": {
        "model": "your-model",
        "api_key_env": "PRISM_LLM_API_KEY",
        "base_url": "https://api.example.com/v1",
        "timeout": 120,
        "concurrency_limit": 1
      }
    },
    "task_roles": {
      "extract": "example",
      "summarize_report": "example",
      "debate": "example",
      "adjudicate": "example"
    }
  }
}
```

再在当前进程环境中提供：

```console
set PRISM_LLM_API_KEY=从本地受保护 secrets 读取的值
```

官方 SDK 会原样接收 `base_url`；PRISM 不自行拼接 HTTP 路径或解析 SSE。

## 6. 配置 Graphiti（可选）

Graphiti 不是 WebUI 启动的必要条件。默认运行使用本地离线后端。

启用 Graphiti 时，使用项目自有原生 Neo4j launcher、独立 home/data/logs/run 目录和 loopback 专用端口。Docker / Docker Compose 不属于 PRISM 路线。

示例配置只保存环境变量名：

```json
{
  "graphiti": {
    "enabled": true,
    "uri": "bolt://127.0.0.1:7688",
    "database": "neo4j",
    "group_id": "neo4j",
    "username_env": "",
    "password_env": "PRISM_GRAPHITI_PASSWORD",
    "timeout": 30
  }
}
```

## 7. 启动 WebUI

```console
python -m prism.webui
```

默认访问地址：

```text
http://127.0.0.1:8765
```

如果要指定端口：

```console
python -m prism.webui --host 127.0.0.1 --port 8765
```

启动命令不会自动打开浏览器；请手动访问上述地址。

## 8. 页面说明

```text
/          案例主页、历史 snapshot、Plotly 时间线和节点证据详情
/debate    多视角辩论、交叉质询、综合和指定视角追问
/evidence  证据搜索、来源过滤、时间过滤和分页
/materials 追加明确指定的 Markdown/PDF 材料
```

材料页面不会猜测目标案例。必须输入用户明确指定的 `case_id` 和本地文件路径。

## 9. 状态含义

页面中的状态分层显示：

```text
loading  正在请求或处理
ready    页面已准备但尚未执行操作
success  操作或 pipeline 成功
partial  机制完成但存在语义缺口或不完整结果
failure  操作或 pipeline 失败
unknown  当前 facade 没有提供该质量字段
```

真实 LLM 结果还会区分：

```text
mechanism_status
semantic_status
evidence gap
```

`semantic=partial` 不是系统崩溃，也不会被界面包装成成功结论。

## 10. 停止 WebUI

在运行 WebUI 的终端按 `Ctrl+C`。停止后，loopback 端口应被释放；WebUI 不会创建后台常驻服务。

## 11. 常见错误

```text
requires the optional nicegui dependency
```

安装：

```console
python -m pip install -e ".[webui]"
```

```text
optional openai SDK is not installed
```

安装：

```console
python -m pip install -e ".[openai-sdk]"
```

```text
host ... is not a loopback address
```

当前 v1 不支持无认证的远程绑定，只使用：

```text
127.0.0.1
localhost
::1
```

```text
provider / Graphiti / PDF failure
```

界面只展示安全的操作名和错误类型，不展示 API key、prompt、材料正文或绝对路径。请查看当前终端的受控日志和对应项目配置。

## 12. 安全边界

- 不要把真实 key 写入 `config.json`；
- 不要把真实材料、prompt 或 quote 提交到公开仓库；
- 不要把 WebUI 绑定到公网或局域网；
- 不要把 `OfflineGraphBackend` 的结果当成真实 Graphiti 验收；
- LLM 结果始终服从 PRISM 的 evidence/time/source/case 边界。
