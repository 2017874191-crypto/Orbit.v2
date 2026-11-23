# Orbit.v2 本地工作区托管

`orbitd` 在一台机器上管理多个 Orbit Worker。每个工作区有一个常驻 Python 进程和独立的
Session 目录。Go 监听本机 HTTP，处理请求去重、跨工作区准入、执行记录和进程恢复。
Python 通过进程管道接收 Turn，沿 Orbit 的 Scheduler、AgentLoop 和 Tool Registry 执行。

## 启动

在 Orbit.v2 仓库根目录使用 Go 1.24+ 与 Python 3.12 构建。准备两个不同的测试目录以及一份
V2 专用的 Orbit `config.json`，其中包含默认模型及对应 Provider 配置。状态目录与工作区应分开。
如果未安装外部 Memory 插件，在配置中写入 `"memory": {"backend": null}`。

```powershell
uv sync --frozen --no-dev
go build -o orbitd.exe ./cmd/orbitd
$env:ORBITD_TOKEN = [Guid]::NewGuid().ToString('N')
.\orbitd.exe --python .\.venv\Scripts\python.exe `
  --state "$env:LOCALAPPDATA\OrbitV2" `
  --workspace "alpha=C:\projects\alpha" --workspace "beta=C:\projects\beta" `
  --concurrency 1
```

默认读取 `--state` 下的 `config.json`，也可以用 `--config` 指定其他配置文件。工作区 ID 与
目录的绑定会随 SQLite 状态保留。普通的 `orbit` 命令可以独立运行。重复启动同一状态目录时，
进程锁会拒绝第二个管理器。

`orbitd` 默认监听 `127.0.0.1:9062`。在另一个终端使用与管理器相同的 `ORBITD_TOKEN`。

```powershell
$headers = @{ Authorization = "Bearer $env:ORBITD_TOKEN" }
$url = 'http://127.0.0.1:9062'
Invoke-RestMethod "$url/health" -Headers $headers
$payload = @{
  workspace = 'alpha'
  conversation_id = 'readme'
  request_key = 'readme-1'
  text = '读取工作区 README 并总结。'
} | ConvertTo-Json
$turn = Invoke-RestMethod "$url/turns" -Headers $headers -Method Post `
  -ContentType 'application/json; charset=utf-8' -Body $payload
Invoke-RestMethod "$url/turns/$($turn.turn_id)" -Headers $headers
Invoke-RestMethod "$url/turns/$($turn.turn_id)/events?after=0" -Headers $headers
```

| 接口 | 行为 |
| --- | --- |
| `GET /health` | 显示并发额度和已就绪 Worker 的 PID。`0` 表示当前未就绪。就绪表示 Runtime 已装配，Provider 在首次调用时初始化。 |
| `POST /turns` | 新请求返回 `202`。同工作区、同键、同内容返回 `200` 和原 Turn。重用键并改变正文或会话返回 `409`。当整机或工作区额度已满时返回 `429`，此次请求仍可重试。 |
| `GET /turns/{id}` | 返回执行状态与取消请求标记。 |
| `POST /turns/{id}/cancel` | 取消尚未派发的 Turn，或向运行中的 Worker 请求取消。之后查询状态确认结果。 |
| `GET /turns/{id}/events?after=N` | 按序读取最多 200 条已提交事件，下一次查询使用最后一个 `seq`。事件包括工具开始和结束、最终文本、用量和终态。 |

## 故障与恢复

Turn 首先以 `accepted` 落盘，发给 Worker 之前标记为 `dispatching`。Worker 开始执行后，
Turn 进入 `running`，随后进入 `completed`、`failed`、`cancelled` 或 `unknown`。
`completed` 表示 Runtime 返回了成功终态，事件另有工具失败数供判断任务结果。

Worker 崩溃时，管理器保存已经收到的完整事件。缺少终态的已派发 Turn 变为 `unknown`，
同一请求键仍指向该 Turn。新 Worker 就绪后接收新任务。管理器自身重启时继续派发之前落盘的
`accepted`，把旧进程中可能已派发的任务记为 `unknown`。工具写入与管理数据库处于不同事务中，
所以 `unknown` 需要结合文件和事件核对后再提交新任务。

## 运行验收

```powershell
go test ./...
.\.venv\Scripts\python.exe tests/integration/verify_orbitd.py --orbitd .\orbitd.exe
```

验收脚本启动本机模型夹具和两个实际 Worker，用临时工作区运行真实的 `edit_file` 工具。
脚本验证同键重复提交只写一行、额度从 1 改为 2 时两个工作区可以同时执行、取消后 Worker
可继续接单，以及工具写完文件后杀掉 Worker 时旧任务变为 `unknown`、文件只写一次、替换的进程
能够完成新任务。对照组用新请求键主动重放故障任务，检查同一文件是否出现第二次写入。脚本还
检查管理器重启后的持久记录和事件游标。

托管模式当前按单用户可信工作区部署，额度按在途 Turn 计数。事件通过游标拉取，模型输出以
最终文本送达。交互提问、子 Agent、消息渠道和 Cron 继续使用终端或 Gateway 场景。
