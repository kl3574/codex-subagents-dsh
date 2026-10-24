# 受管运行与验收后收尾

每次调用先运行 `manage_dsh.py prepare --config CONFIG --cwd CWD`。返回的独立目录
位于配置的 `DSH_HOME/.codex-dsh-wrapper/runs/`，包含：

| 位置 | 用途与保留规则 |
| --- | --- |
| `scratch/` | 本次运行拥有的临时输入和中间文件；主 agent 明确收尾后删除内容 |
| `evidence/` | 明确保留的证据；不自动删除 |
| `diagnostics/` | 状态与有界诊断日志；收尾后保留 7 天 |
| `run.json` | 有界运行、执行和收尾记录；不含原始任务、回答或凭据 |
| `lease.lock` | 执行与收尾共享的互斥锁 |

最终交付物放入任务指定的项目位置或 evidence，验收前确认交付物不依赖 scratch。
运行目录不会接管已有仓库文件、借用输入或历史散落缓存。原生 session 历史和
wrapper 的 session 元数据不在清理范围内。

`run_dsh.py --run-dir DIR` 使用该目录的身份、诊断位置与状态路径，保持 cwd、
任务字节、profile、patches、模型和权限选择。任务可用 `--task-file -` 从 stdin
传入，也可借用现有输入文件；wrapper 不改写借用文件。子进程可读取
`CODEX_DSH_RUN_DIR`、`CODEX_DSH_SCRATCH_DIR` 和 `CODEX_DSH_EVIDENCE_DIR`。
同 session 后续任务每次 prepare 新目录，保留相同的 DSH_HOME、cwd、profile 和
有序 patches，再传 `--session-id`。一个 run_dir 只能执行一次。

Harness 完成仍为 `pending_acceptance`。主 agent 独立验收并交付后执行：

```bash
python3 scripts/manage_dsh.py finalize --run-dir DIR --disposition accepted --delivered
```

明确放弃任务时用 `--disposition abandoned`。预览加 `--dry-run`，应用预览时可加
`--expected-plan TOKEN`；文件变化使旧 token 失效。直接 finalize 同样重新验证
当前目录、运行状态与进程身份。它只清空 scratch，不删除目录与保留记录。
重复相同决定是幂等的；更改已经记录的接受/放弃决定会被拒绝。

执行与收尾共用独占 lease；活动或 UNKNOWN 运行不能破坏性收尾。已停止的失败、
取消或超时运行仍保留恢复材料，直到主 agent 明确决定。状态 JSON 的终态字符串
本身不能授权删除。崩溃后无法确认停止的运行保持 UNKNOWN，不自动重放模型请求。
文件链接、目录替换与特殊文件会被拒绝；并发写入应遵循同一 lease，任意绕过锁的
特权进程不属于保证范围。已有进程组清理规则仍适用，逃离进程组的后台进程不受保证。

收尾记录分别保存 disposition、execution 和 cleanup。清理部分失败时保持
`closeout_pending` 与错误码，修复文件系统原因后重试相同命令；成功后才开始
7 天保留期。执行结果不会因清理结果被改写。未经 `--run-dir` 的旧调用仍可使用，
返回 `artifacts.state=unmanaged`，其文件不纳入自动清理。
