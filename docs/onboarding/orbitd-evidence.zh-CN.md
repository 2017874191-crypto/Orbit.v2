# Orbit.v2 实验与五分钟演示

在 Windows 上准备 Go 1.24+ 和 `uv`，从 V2 的 `local-worker` 分支检出代码后在根目录
执行演示。脚本会根据锁文件准备 Python 3.12 环境，构建 Go 管理器，并用临时工作区和本机
模型夹具运行实际的 Worker。首次执行可能需要下载依赖。

```powershell
.\scripts\demo-orbitd.ps1
```

Go 未加入 `PATH` 时，可传入 `-GoExecutable 'C:\path\to\go.exe'`。已有环境和二进制时
可以直接运行[托管验收脚本](orbitd.zh-CN.md#运行验收)。

## 可靠性对照

| 场景 | 实测 | 对照与量法 |
| --- | --- | --- |
| 客户端提交后未读回执 | 同键重试返回 HTTP 200，共 1 个 Turn、1 次工具执行、1 次文件写入 | 同一对话和正文用两个新键提交，得到 2 个 Turn、2 次工具执行、2 次写入。比对模型夹具发出的工具调用与文件中的 `written` 行数。 |
| 两个工作区争用额度 | 额度为 1 时，第二个提交返回 HTTP 429。第一个完成后可重试，重试获准，运行峰值为 1 | 额度为 2 时，运行峰值为 2。按 SQLite 事件序号比较 `running → completed` 区间，两条 `running` 都先于第一个 `completed`。 |
| 文件写完后 Worker 消失 | 终止 Worker 后，旧 Turn 为 `unknown`，文件有 1 行 `written`，替换进程完成新 Turn，另一个工作区的 PID 保持原值 | 用新请求键主动重放同一个文件操作，文件变成 2 行 `written`。这一分支用于观察自动重放可能造成的副作用。 |
| 管理器重启 | 同一 SQLite 状态目录保留原 Turn ID 和 7 条事件，原游标仍返回相同的 3 条事件 | 用原工作区和空状态目录启动另一个管理器，查询原 ID 返回 HTTP 404。 |

脚本还核对了取消后继续复用 Worker，以及读取临时 README 后在回复中返回真实校验码。表中数据
来自确定性的本机模型响应，执行使用 Orbit 的 Provider 适配器、Scheduler、AgentLoop 和文件工具。

## Go 管理器中途退出

构建 `orbitd.exe` 后，可独立运行这项验收。脚本使用本机模型夹具、真实文件工具和独立的临时
状态目录，强制结束的是这次测试启动的 Go 进程。

```powershell
go build -o orbitd.exe ./cmd/orbitd
.\.venv\Scripts\python.exe tests/integration/verify_orbitd_manager_crash.py --orbitd .\orbitd.exe
```

- **接单后、派发前。** 用 Worker 的真实文件锁阻止它就绪。提交返回 `202`，唯一落盘事件是
  `accepted`。强制结束 Go 后释放锁，用同一状态目录启动新管理器，核对原 Turn ID 能完成且
  文件只写一次。
- **工具写完、终态前。** 让模型夹具暂缓返回最终回复，看到 `edit_file` 完成事件和文件写入后
  结束 Go。重启前等待旧 Worker 退出，重启后核对事件、原 Turn ID、新 Worker 和文件行数。

2026-11-23 在 Windows 11 实测通过。第一组保留原 `accepted` 事件和 Turn ID，随后写入一次并
进入 `completed`。第二组保留故障前的 5 条事件，追加原因是 `manager_restarted` 的
`unknown` 终态。同键重试返回 HTTP 200 和原 Turn ID，文件仍只有一行 `written`。旧 Worker
已退出，更换 PID 的新 Worker 成功完成另一条任务。

第二组还会真实调用 [`orbitd inspect`](orbitd.zh-CN.md#核查-unknown-任务)，核对它能读出
`manager_restarted` 和已成功完成的 `edit_file`，并逐条比较核查前后的历史事件。

## 真实模型冒烟

本机使用 Qwen3-1.7B Q4_K_M 和 llama.cpp b12651，通过 OpenAI-compatible 接口接入 Orbit 的
`custom` Provider。[模型权重](https://huggingface.co/ggml-org/Qwen3-1.7B-GGUF) ·
[推理服务版本](https://github.com/ggml-org/llama.cpp/releases/tag/b12651)

2026-11-23 实测通过。Turn `5e34918d9a9d893360ac17cde11cd058` 以 `completed` 结束，
模型的总结带回了仅写在临时 README 中的随机校验码。Go 共落盘 15 条事件，其中有 5 次成功的
`read_file` 开始及结束记录，工具失败数为 0。重复读取反映这次模型的工具选择，事件保留了每次调用。

模型服务已就绪时，从提交到客户端读到终态为 29.76 秒。该样本包含 Worker 的首次 Provider
调用、实际推理和事件查询。下文的性能表使用固定模型夹具单独测量托管路径。

在 Windows Vulkan 版 llama.cpp 中，先用 `llama-server.exe --list-devices` 确认设备名。
本次 RTX 4060 Laptop GPU 对应 `Vulkan1`。

```powershell
.\llama-server.exe -m .\Qwen3-1.7B-Q4_K_M.gguf --host 127.0.0.1 --port 16384 `
  --alias qwen3-1.7b --jinja --reasoning off --ctx-size 8192 --parallel 1 `
  --device Vulkan1 --n-gpu-layers 99
```

使用以下 V2 专用配置。此次任务只暴露 `read_file`，用来验证真实模型调用、工具执行和结果回填。

```json
{
  "agents": {
    "defaults": {
      "model": "openai/qwen3-1.7b",
      "provider": "custom",
      "maxTokens": 512,
      "maxToolIterations": 4,
      "contextWindowTokens": 8192,
      "temperature": 0
    }
  },
  "providers": {
    "custom": {"apiKey": "local-only", "apiBase": "http://127.0.0.1:16384/v1"}
  },
  "tools": {
    "disabledTools": ["exec", "web_search", "web_fetch", "write_file", "edit_file", "list_dir", "grep", "find", "skill_read"]
  },
  "memory": {"backend": null},
  "callEfficiency": {"mode": "off"},
  "tracing": {"enabled": false}
}
```

运行[README 冒烟命令](orbitd.zh-CN.md#运行验收)，可用 `--smoke-report` 保存事件 JSON。

## 五分钟演示顺序

开场可以从一个故障讲起：“我在一台机器上同时运行两个工作区的 Agent。其中一个已经改完文件，
却在回报结果前退出。我需要查清执行记录、让它恢复接单，并避免重试时重复改文件。”

1. **第 1 分钟**启动验收脚本，介绍 Go 负责接单、跨工作区准入、记账和拉起进程，Python 负责模型、
   工具和 Session。说明两个工作区有独立 Worker，使用一个管理器。
2. **第 2 分钟**对照 `Dedup` 和 `Quota` 的输出。请求键解决客户端重试，整机额度解决两个
   Worker 分别看不到对方在途任务的问题。
3. **第 3–4 分钟**展示 `Crash` 输出。指明注入位置是 Go 已收到 `edit_file` 的完成事件、模型
   仍未返回最终结果时。用一行与两行的差异解释为什么只重启进程，并保留旧 Turn 的 `unknown`。
4. **第 5 分钟**展示 `Restart` 和性能表。说明请求键保护提交入口，工具写文件与 SQLite 提交各自
   独立，`unknown` 下的重试需要先看事件和文件。

被问到 Go 的必要性时，可以说明单工作区继续走 Orbit 的 Python 入口。多个 Worker 需要共享准入
和恢复记录时，`orbitd` 提供独立的本机管理进程。Go 在这里承担进程生命周期和 HTTP，原有
AgentLoop 继续由 Python 执行。面试时用下面的测量数据说明这层管理的实际成本。

## 复测性能

```powershell
.\.venv\Scripts\python.exe tests/integration/benchmark_orbitd.py --orbitd .\orbitd.exe
```

两路各使用一个工作区、同一个 `orbit.worker` 入口，提交同样的短文本，经过同一个本机模型夹具，
每个 Turn 做一次模型调用。交替运行 3 轮，每轮每路预热 5 条并采集后续 20 条。脚本输出汇总和
全部样本的 JSON，环境信息包含 Python 版本和 Go 二进制 SHA-256。

启动计时从创建进程前开始，到客户端看到 Worker 的 `ready` 帧或 `/health` 就绪为止。预热
Turn 从提交前开始计时。直连读管道事件，托管通过 HTTP 按游标查询，查询间隔设为 2 ms。托管端
数字包含 HTTP 请求与发现事件的等待。内存是在每轮末尾进程空闲时读取的工作集快照，按进程列报，
不含平台启动器与模型夹具。冷启动只有 3 次，表中报告中位数。

实测于 2026-11-23，Windows 11（26300）、Intel Core i7-14700HX、Python 3.12.7、
Go 1.27.4。本次构建使用 `3c2a893` 的托管实现。每路共 60 条预热后的 Turn。

| 指标 | Python 直连 | Go 托管 |
| --- | ---: | ---: |
| 进程启动到就绪 P50 | 1,744.87 ms | 2,119.88 ms |
| 提交到 HTTP 202 P50 / P95 | — | 31.13 / 68.07 ms |
| 提交到看到 `running` P50 / P95 | 0.98 / 2.41 ms | 83.23 / 128.12 ms |
| 提交到看到 `completed` P50 / P95 | 164.46 / 203.87 ms | 287.93 / 334.33 ms |
| Python Worker 空闲工作集 P50 | 196.46 MiB | 196.38 MiB |
| Go 管理器空闲工作集 P50 | — | 20.05 MiB |

这组负载下，托管的冷启动中位数增加约 375 ms，完成延迟 P95 增加约 130 ms。这个差值反映
两种客户端路径的整体差异，包含持久化和轮询。测量范围是一台机器、一个 Worker、顺序短 Turn，
支持讨论本机托管成本。真实模型冒烟是独立的功能验证，其耗时单独记录。
