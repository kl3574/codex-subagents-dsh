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
这些路径不扩展 Harness 的沙箱权限。启动器负责写临时输入，主 agent 可保存已返回的
验收证据；Harness 文件产物仍写入已获准的工作区。如果 `workspace-write` 禁止写入
工作区外的受管目录，按权限失败报告，保持现有策略；不要把 evidence 路径当成额外
可写授权。stdout 返回值也可以由主 agent 独立验收、留存并完成收尾。
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

## 诊断回收

```bash
python3 scripts/manage_dsh.py gc --config CONFIG --dry-run
python3 scripts/manage_dsh.py gc --config CONFIG --expected-plan TOKEN
```

可直接省略 `--expected-plan` 执行当前重新验证的计划。默认每次检查 16 个运行，
`--limit` 接受 1–64。每次 prepare 和成功 finalize 自动检查最多 4 个运行；
无后台服务。预览不创建或修改文件，也不推进游标。

工具创建的运行 ID 进入固定宽度候选索引，轮转游标确保活动或被保留的早期运行
不永久挡住后面的到期运行。每次扫描仅读取本批 ID；每个诊断树最多检查 10,000
项、64 层。索引不授予删除权限，每个候选仍必须通过独立目录、manifest、lease、
身份和到期检查。正常登记仅增长 32 字节/运行，游标固定 8 字节，不记录文件列表或任务内容。
索引锁仅覆盖短暂的登记和游标操作；耗时扫描/删除不阻塞登记。登记处理短写，
写失败回滚未提交尾部，后续写操作可截去崩溃留下的不完整尾部；预览不修复文件。
finalize 会幂等复核登记，失败后可重试同一 finalize 修复；若登记已落盘但 manifest
写入中断，可能保留一个重复候选，逐运行校验仍保证安全。索引损坏或不可写会单独
报告 housekeeping 警告；不扫描旧目录补齐，不更改执行结果。

普通诊断在 `closed_at + 7 天` 起可回收。在 `diagnostics/` 内放置 `.keep` 可保留
整棵诊断树；长期证据优先放在 evidence。GC 跳过未关闭、UNKNOWN、活动/锁定、
未到期、受保护或身份不符的运行，并给出原因。成功后留下有界回收记录，重复扫描
跳过已回收目录。部分失败保存状态、已删除数量和错误码，保留剩余文件，重试 GC
累积已提交的删除计数。收尾记录写入失败同样报告可重试状态；写入中断或文件系统
故障可能使最后一批计数无法持久化，不能把回执计数当作文件系统事务审计。

自动回收错误位于 `housekeeping.warnings`，prepare/finalize 的有效主结果保持不变。
手动 GC 或 finalize 的部分删除失败退出码为 6；参数或安全校验拒绝为 2。
安装与升级自身不会触发 GC，也不会清理历史未受管文件。
