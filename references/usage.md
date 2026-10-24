# 操作、进度与错误语义

本参考面向已经安装技能的执行者与主 agent。默认技能目录取自已加载技能的 base
directory；配置目录默认是 `Path.home()/.dsh-codex`。本文用 `$HOME` 和
`<skill-dir>` 表示这两个位置，不绑定某台机器或某个用户名。

## 1. 安装结果与支持范围

- 运行器：`<skill-dir>/scripts/run_dsh.py`。
- 桥接配置：`$HOME/.dsh-codex/bridge.json`。
- 独立 Harness home：默认 `$HOME/.dsh-codex`，其中还有 `profiles/`、`logs/`、
  `backups/`、`install-receipt.json`。运行器只为子进程设置 `DSH_HOME`，不修改
  Codex 的全局配置、shell rc 或日常 `~/.dsh`。
- 默认 profile：`codex-dsh`；审查 profile：`codex-dsh-review`。两者都由官方
  headless 模板初始化，模型选择均为 `deepseek-official / deepseek-flash / max`。
- 支持 Linux/POSIX、Python 3.10+ 和已安装的 Node 22.19+。Windows 上运行器明确
  返回 `PLATFORM_NOT_VERIFIED`；macOS 未验证。安装器只支持 Linux/POSIX。

安装、更新、迁移和可选全局路由的准确命令见仓库 [README](../README.md)。日常执行
不需要重新运行安装器，也不要重新初始化已有 profile。

启用全局路由时，v0.1.1 会从受管原生启动器移除根级 `model`、
`model_reasoning_effort`、`service_tier`，让它使用 Codex 解析后的默认值。
原生模型选择依次来自显式 spawn 参数、全局 `[agents]` 默认、父会话；实际
Harness 任务仍使用上面的 DeepSeek profile。已有未受管自定义角色默认保留；
检查原文件后，可用 `--enable-global-routing --adopt-existing-launchers` 显式
备份并迁移 `default`、`explorer`、`worker`。重复运行保持幂等。

原生启动器的模型被账户拒绝时，Harness 尚未启动。先检查原生角色、全局
`[agents]` 默认、项目级角色或本次 spawn 覆盖；不要把该错误报成 DeepSeek API
失败，也不要通过自动重放业务请求来处理。

## 2. bridge schema 2

安装器写入的新配置形如（路径必须展开为绝对路径；下面是占位示例）：

```json
{
  "schema_version": 2,
  "dsh_command": ["/absolute/path/to/node", "/absolute/path/to/@deepseek-ai/dsh/lib/bin.js"],
  "dsh_home": "/absolute/path/to/.dsh-codex",
  "default_profile": "codex-dsh",
  "default_patches": [],
  "hard_timeout_seconds": null,
  "heartbeat_seconds": 5,
  "stall_warning_seconds": 300,
  "shutdown_grace_seconds": 10,
  "finalization_grace_seconds": 10,
  "log_dir": "/absolute/path/to/.dsh-codex/logs"
}
```

| 字段 | 默认 | 语义 |
|---|---|---|
| `schema_version` | `2` | schema 1 仍可被运行器读取；安装器可显式迁移 |
| `dsh_command` | 自动发现或显式指定 | 绝对解释器 + 绝对 dsh 入口数组；不会在运行时重新安装 |
| `dsh_home` | 配置目录 | profile、session 和运行日志所在的私有 Harness home |
| `default_profile` | `codex-dsh` | 未传 `--profile` 时的 profile |
| `default_patches` | `[]` | 每次运行都先应用的有序 patch 列表；不自动加入 `max.yml` |
| `hard_timeout_seconds` | `null` | 总墙钟硬预算；`null` 表示不设隐式总时限 |
| `heartbeat_seconds` | `5` | supervisor 发布状态心跳的间隔，仅证明监督进程仍能写入 |
| `stall_warning_seconds` | `300` | 无允许列表事件达到阈值时给出诊断警告；不终止、不定罪 |
| `shutdown_grace_seconds` | `10` | 取消/硬预算/信号后，等待进程组优雅退出的窗口，之后强制清理 |
| `finalization_grace_seconds` | `10` | 收到 completed+final 后等待 flush/退出/管道关闭的窗口 |

schema 1 的 `timeout_seconds` 是旧的隐式硬预算。安装器只有在显式
`--migrate-progress` 时才移除默认 300 秒并迁移到 schema 2；非默认旧值会保留到
`hard_timeout_seconds`。运行器不会把 schema 2 中残留的 `timeout_seconds` 重新解释为
硬截止。手工改 bridge 时保留完整 JSON 对象，避免丢失其他用户字段。

## 3. profile、patch 与选择顺序

每次调用先创建一个新受管运行（同 session 后续调用也创建新目录）：

```bash
python3 <skill-dir>/scripts/manage_dsh.py prepare \
  --config "$HOME/.dsh-codex/bridge.json" --cwd /absolute/workspace
```

下列示例中的 `/absolute/owned/run` 使用本次 prepare 返回的 `run_dir`。
临时输入放入其 scratch；借用文件可继续从原路径只读传入。
执行结束后由主 agent 验收并 finalize，命令与保留规则见
[产物收尾](artifact-lifecycle.md)。默认运行：

```bash
python3 <skill-dir>/scripts/run_dsh.py \
  --config "$HOME/.dsh-codex/bridge.json" \
  --cwd /absolute/workspace --task-file /absolute/task.txt --run-dir /absolute/owned/run
```

选择另一个已有 profile（只影响本次）：

```bash
python3 <skill-dir>/scripts/run_dsh.py \
  --config "$HOME/.dsh-codex/bridge.json" \
  --cwd /absolute/workspace --task-file /absolute/task.txt \
  --run-dir /absolute/owned/run --profile codex-dsh-review
```

应用一个或多个按序 patch（可重复）：

```bash
python3 <skill-dir>/scripts/run_dsh.py \
  --config "$HOME/.dsh-codex/bridge.json" \
  --cwd /absolute/workspace --task-file /absolute/task.txt \
  --run-dir /absolute/owned/run --patch "$HOME/.dsh-codex/patches/max.yml"
```

顺序是：所选 profile 的 bundle 层与 `cordis.patch.yml`，然后
`default_patches`，再按命令行出现顺序应用本次 `--patch`。指定的另一个 profile
仍继承 `default_patches`。本次覆盖不会写回 profile；需要完全不同的默认集合时使用
另一个 `--config`。`patches/max.yml` 只是可选覆盖，不在默认列表中。

持久修改模型：编辑 `$HOME/.dsh-codex/profiles/<profile>/cordis.patch.yml`。
该版本的 `config` 是整块替换，不是字段级深合并；覆盖模型时保留
`provider`、`model`、`reasoningEffort` 三项。`--from-default-profile` 会替换目标
profile，只用于首次创建或明确的官方重建，日常启动器不使用它。

## 4. 调用、状态文件与取消

`--task-file -` 显式从 stdin 读取完整 UTF-8 任务。`--timeout` 接受有限正数秒，是
本次运行的硬预算。任务通过文件或 stdin 传输，不用 shell 拼接。

受管调用的状态文件由 prepare 返回，并由 `--run-dir` 自动选择：

```bash
python3 <skill-dir>/scripts/run_dsh.py \
  --config "$HOME/.dsh-codex/bridge.json" \
  --cwd /absolute/workspace --task-file /absolute/task.txt \
  --run-dir /absolute/owned/run
```

状态文件必须尚不存在且父目录已存在；运行器创建权限 0600 的有界快照，并拒绝
符号链接、已存在路径和不可写父目录。主 agent 可在运行中查询：

```bash
python3 <skill-dir>/scripts/manage_dsh.py status \
  --status-file /absolute/owned/run/diagnostics/status.json
```

需要取消时，由本次 owner 写一个与 `run_id` 和目标进程身份绑定的单次请求：

```bash
python3 <skill-dir>/scripts/manage_dsh.py cancel \
  --status-file /absolute/owned/run/diagnostics/status.json --reason user_cancelled
```

`manage_dsh.py` 从不直接 kill PID：它校验状态文件、supervisor/child 身份和心跳
新鲜度，只写控制请求；真正拥有子进程的 wrapper 读取请求并执行优雅关停。取消请求
只能写一次，且只在状态为 `RUNNING`、身份已验证、心跳新鲜时接受。状态文件是观察
通道，不是任务进度或验收证明。

`manage_dsh.py` 退出码：0 已读取状态或已写取消请求（状态仍可能是 `UNKNOWN`）；
2 参数/配置错误；3 状态文件缺失、损坏、不可读或不安全；4 取消被拒绝（状态非
live/fresh）；5 控制请求无法写入或已存在；6 产物清理部分失败。
旧的未受管调用仍可指定独立 `--status-file`；受管调用若显式传入状态路径，
必须等于 prepare 的返回值。其输出是单行 JSON，`status` 子命令的
`state` 可能是 `RUNNING`、`COMPLETED`、`FAILED`、`CANCELLED`、`TIMED_OUT`、
`INTERRUPTED`、`CLEANUP_FAILED` 或 `UNKNOWN`。`UNKNOWN` 只是观察不可确认，不等于
任务失败，也不是重跑理由。

## 5. 心跳、活动、实质进展与硬预算

以下信号必须分开报告：

- **心跳（heartbeat）**：supervisor 仍能按 `heartbeat_seconds` 写入状态。它只证明
  监督进程存活，不证明 Harness、模型或任务仍在推进。
- **活动（activity）**：允许列表中的 NDJSON 生命周期事件、工具调用/结果提交、
  step/turn 边界。它比心跳更具体，但仍只是弱观察，不证明任务语义进展。
- **实质任务进展（substantive progress）**：实际产物、文件差异、命令结果或验收
  证据。只能由主 agent 独立检查，不能从心跳、CPU、日志行数或工具调用次数推断。

默认 `hard_timeout_seconds=null`：不设隐式总墙钟截止。运行会继续，直到完成、
owner 取消、收到信号、显式 `--timeout` 到期，或真实的流/资源失败。超过
`stall_warning_seconds` 没有允许列表事件时，只产生 `SUSPECTED_STALL` 观察警告，
继续同一进程；状态中的 `supervision.stall` 会记录 suspected/active 和恢复点。它
不会把运行标成失败，也不会自动判定语义停滞。

显式 `--timeout` 和配置的 `hard_timeout_seconds` 才是硬预算；到点会终止本次拥有的
进程组并返回超时错误。`shutdown_grace_seconds` 是关停宽限，`finalization_grace_seconds`
是 completed+final 之后的收尾宽限，两者都不是业务超时。

SDK 实时 token 计数、逐 token 交互控制、隐藏推理和自动语义停滞定罪不是当前
headless 能力。不要把 `--json` 投影或状态快照伪称为完整内部状态。

已验证的 dsh 0.2.1-rc.2 中，`--json` 的 text/thinking 来自已提交的
`assistant/message`，不是实时 token；JSON 模式也不提供普通模式的实时 stderr 推理流。
因此模型仍在生成时可能长时间没有新事件。DeepSeek provider 另有默认 300 秒的
`streamIdleTimeoutMs`，由上游流事件或心跳注释续期；这是传输层保护，独立于 wrapper
的总时限和静默告警。本技能保留该保护，不把传输超时报成语义停滞，也不自动重试请求。

与原生 subagent 的等待方式相同，工具的一次等待窗口结束后，继续观察同一运行。
有独立工作时主 agent 可以继续做；依赖子任务结果时再等待。当前 headless 没有原生
subagent 的交互式消息能力，不能把 session 恢复当作向正在运行的任务追加指令。

| 观察到的情况 | 策略 |
|---|---|
| 心跳正常但无新事件 | 核验进程身份、当前工具与产物；记录未知进展，继续观察 |
| 有工具调用或步骤完成 | 记录活动；另查产物是否朝验收条件推进 |
| 工具反复执行却无有效产物 | 主 agent 检查具体差异、错误和重复操作；有证据后再决定取消或调整后续任务 |
| 状态过期、读取失败或身份不匹配 | 返回 `UNKNOWN`，查询原句柄；不重启、不按旧 PID 直接 kill |
| 工具失败但 Harness 仍运行 | 保留错误，观察是否恢复；不把每个工具错误当整轮失败 |
| 认证、传输或 Harness 明确失败 | 保留失败和已有产物，确认退出状态；不自动重放业务请求 |
| stdout 已关闭且无 final，进程仍活着 | 已无法接收最终协议结果，报 `STDOUT_EOF_WITHOUT_FINAL` 并有界清理 |
| final 已有但进程或管道不退出 | 进入收尾宽限，必要时清理；保留答案并单列清理异常 |
| 用户取消、显式预算或资源上限到达 | 按对应原因停止拥有的进程，分别报告取消、预算或资源错误 |

## 6. 完成、结果与清理异常

成功完成要求同一 session 中有正确配对的 turn_start/turn_end、`turn_end.reason.kind`
为 `completed`、其后有可接受的 final，并且 Harness 进程退出 0。`harness_completed=true`
只表示执行层完成，不证明任务满足验收条件。主 agent 必须独立检查实际产物。

结果字段的结构区分：

- `protocol_completed`：严格协议完成（session + completed turn + final + 无协议错误）。
- `result_available`：已捕获有效 final 和 completed turn，即使之后退出码、清理或
  一致性检查失败。
- `answer`：已捕获的最终答案；清理异常不丢弃它。
- `turn_end`：最后一个 turn 的结束原因；非 `completed` 表示该 turn 失败。
- `tool_errors`：有界的工具错误摘要；即使后来修复，也要在报告中说明。
- `finalization` / `cleanup`：收尾等待和拥有的进程清理结果。

如果 completed+final 已出现，之后清理或 finalization 异常，wrapper 保留 `answer`
和 `result_available`，把清理问题作为独立错误/退出码 13 报告；`harness_completed`
为 false。不要因为清理异常丢弃已得到的任务结果，也不要反过来把结果当作验收通过。
`finalization.timed_out`、`cleanup.status`、`cleanup.diagnostics` 和 `error.category`
必须分别报告。completed+final 之后进程或管道仍不退出时，wrapper 有界等待后强制
清理并保留结果。

## 7. 恢复与不重跑

恢复使用上次结构化输出中的真实 `session_id`，并保持相同 DSH_HOME、cwd、profile
和有序 patches。先 prepare 新运行，再使用它的新目录：

```bash
python3 <skill-dir>/scripts/run_dsh.py \
  --config "$HOME/.dsh-codex/bridge.json" \
  --cwd /absolute/workspace --task-file /absolute/followup.txt \
  --run-dir /absolute/new/owned/run --session-id session-REPLACE-WITH-RETURNED-ID
```

启动器先检查自己记录的 session 选择，再由 Harness 执行官方兼容性检查。外部已有
session 只有在本次 DSH_HOME 下且符合官方 headless 的 cwd/preset/来源限制时才可
采用；不假定跨目录、跨 preset 或任意 profile 可以恢复。原 session 没有启动器
记录时，把这项检查标为外部未知，以 Harness 实际接受/拒绝为准。

监控丢失、状态文件过期或取消请求不明确时，先读取同一运行句柄的状态和进程身份，
不要启动第二个 session 重跑未知状态的模型业务请求。恢复观察不是重新提交业务请求。

## 8. 错误与退出码

wrapper 退出码：

| 码 | 含义 |
|---|---|
| 0 | Harness 完成（仍不等于任务验收通过） |
| 2 | 配置、参数或输入路径错误 |
| 3 | Harness 启动失败 |
| 4 | 认证缺失或被拒绝 |
| 5 | 协议/事件流错误 |
| 6 | 显式硬预算或配置硬预算到期 |
| 7 | 调用方中断/信号 |
| 8 | Harness 执行失败 |
| 9 | 大小/事件数上限 |
| 10 | 启动器记录的 session 选择不匹配 |
| 11 | 本地 I/O、supervision 或状态通道错误 |
| 12 | owner 请求取消 |
| 13 | finalization/进程清理异常，已捕获的结果仍保留 |

查看结构化 `error.category` 和 `error.code` 获取安全细分类别；不要分享原始启动
错误、stderr、认证头或完整环境。认证/模型请求失败不要自动重试；先确认实际
进程状态和已有 session。`wrapper_exit_code`、`process_exit_code`、`harness_completed`、
`protocol_completed`、`result_available` 和任务验收要分别报告。

## 9. 日志、上限与隐私边界

默认上限：任务 1 MiB、stdout 16 MiB、stderr 4 MiB、单行 1 MiB、final answer
256 KiB、每次 wrapper 日志 2 MiB、事件 100000 个。bridge.json 可设置
`max_task_bytes`、`max_stdout_bytes`、`max_stderr_bytes`、`max_line_bytes`、
`max_answer_bytes`、`max_log_bytes`、`max_events`。日志预算耗尽时轮转并降级为摘要，
标记日志不完整，不因此终止健康任务。其他协议/输入输出资源上限可终止执行，
不能把截断内容当完整结果。

wrapper 日志和状态文件只保存有界结构化元数据；原始任务、推理、工具输入/结果、
完整 stderr 和认证头不写入。它们仍不是完整轨迹：上游 `--json` 投影会截断过长
字符串、省略未建模事件，`log_complete=false`、`log_degraded`、`log_truncated` 或
`upstream_truncated` 时必须如实说明范围。Harness 自己的 session/startup 日志由
官方程序管理，可能包含任务和原始错误，保留在私有 DSH_HOME 内，不要直接分享。
最终 answer 是任务输出，仍可能含业务敏感信息。

## 10. 凭据与诊断

本机安装版本的官方 headless API-key 来源优先级：启动环境 > `<cwd>/.env` >
`<DSH_HOME>/.env`。已导出的空值会遮蔽 `.env` 后备来源；`DSH_*` 控制变量应在启动
环境设置，不写入 `.env`。GUI 保存的登录状态不代表 headless API key 可用。安装器
不读取或复制 `.env`；不要在聊天、日志或 receipt 中输出密钥。

只读诊断（不发模型请求）：

```bash
python3 <skill-dir>/scripts/doctor.py \
  --config "$HOME/.dsh-codex/bridge.json" --cwd /absolute/workspace
```

需要持久配置时，用户可自行编辑 `$HOME/.dsh-codex/.env`，只写该版本允许的普通
凭据字段并设为 0600。终端已有空变量时先修正该变量，否则 `.env` 不生效。不要通过
清除代理、关闭沙箱或放宽全局权限来绕过诊断失败。网络 HEAD、凭据存在、模型认证和
任务验收是不同证据；`doctor.py` 的 schema2/status/cancel 相关结果明确标为
`NOT_RUN`，不能据此宣称真实 API 验收通过。

## 11. 测试与验收状态

无需 `dsh`、网络或模型的安装器隔离测试：

```bash
python3 <skill-dir>/scripts/test_install.py -v
```

wrapper 边界测试（可选官方 `--dump-config` 组合检查，仍不发模型请求）：

```bash
python3 <skill-dir>/scripts/test_run_dsh.py -v
python3 <skill-dir>/scripts/test_run_dsh.py \
  --official-config "$HOME/.dsh-codex/bridge.json" -v
```

`--dump-config` 只证明配置组合，不证明 provider/model/effort 已由服务器接收。
v0.1.0 发布前另通过原生 Codex 启动器验证真实 DeepSeek 普通任务、同 session 后续任务、
静默告警后继续完成、运行状态查询与主动取消；主 agent 独立核对产物哈希及清理结果。
日志耗尽、协议破损和 final 后悬挂由隔离模拟测试覆盖，不声称已在真实 API 中诱发。
离线测试、配置检查和真实 API 验收证据分别报告。

## 12. 官方依据

- [Codex skills](https://learn.chatgpt.com/docs/build-skills)
- [Codex subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)
- [dsh launcher](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/cli/reference/README.md)
- [headless 与 NDJSON](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/headless/README.md)
- [Harness testing](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/testing.md)

上游 master 可能更新；实际安装版本的 help、schema 与包内源码是适配依据。
