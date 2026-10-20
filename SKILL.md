---
name: codex-subagents-dsh
description: 把明确、限定范围的子任务通过普通 Codex subagent 交给独立 DeepSeek Harness headless 进程；支持持久/单次 profile、有序 patch、session 恢复、硬预算与可选状态观察。
---

# codex-subagents-dsh

主 agent 定义任务、权限和验收；普通 subagent 只启动本技能的
`scripts/run_dsh.py` 并回传结果。Harness 拥有自己的模型、上下文、工具和 session。
完整操作、进度/错误语义和恢复规则见 [references/usage.md](references/usage.md)。

## 主 agent

1. 明确任务、绝对 cwd、允许的操作、验收条件，以及可选的 profile、按序 patch、
   session_id、timeout 和状态文件。默认配置 `~/.dsh-codex/bridge.json`，profile
   `codex-dsh`，模型 `deepseek-official / deepseek-flash / max`，
   `default_patches=[]`。
2. 检查当前会话真实提供的原生 subagent 工具 schema；使用普通 launcher，支持
   `fork_turns="none"` 时使用它。工具名/字段变化时按实际 schema 调整，不依赖自定义
   角色。
3. 发送完整消息；技能位置取自已加载技能的 base directory，路径按
   `Path.home()` 动态解析：

   ```text
   执行角色：你是普通 Codex subagent，直接执行本任务，不再递归委派。
   先读技能：<skill-dir>/SKILL.md
   配置：<home>/.dsh-codex/bridge.json
   cwd：<绝对工作目录>
   Harness 任务：<完整任务>
   本次 profile：<省略表示默认>
   本次 patches：<按应用顺序列出绝对路径；默认空>
   session_id：<新会话省略；恢复时用上次返回值>
   timeout：<显式硬预算秒数；省略时使用 bridge 的 hard_timeout_seconds（默认 null）>
   status_file：<主 agent 指定的新绝对路径；建议提供以便持续观察>
   允许操作：<具体文件/目录与允许的命令>
   验收条件：<实际产物或可核验结果>
   将任务写入 0600 的 UTF-8 临时文件，直接运行 scripts/run_dsh.py；
   等待结束并返回完整结构化结果，不要代写目标产物。
   ```

4. 有其他独立工作时继续推进，依赖结果时等待同一 subagent。调用工具的等待窗口到期
   只表示本次观察结束，不是任务失败。运行中可用 `scripts/manage_dsh.py status --status-file PATH`
   查询；需要时用 `cancel --status-file PATH --reason user_cancelled` 请求取消。
   状态查询、取消和恢复观察都不重新提交任务；不要启动第二个 session 重跑。
5. 独立检查实际文件、差异或命令结果，分别报告 **Harness 完成状态**、
   **任务验收状态** 和 **未解决的工具错误**。`harness_completed` 只表示执行层完成，
   不证明验收通过；认证或模型请求失败时不要自动重试业务请求。

没有原生 subagent 工具时，可以安装、诊断和直接运行 Harness，但原生委派验收标为
`NOT_VERIFIED`；直接执行证据不能替代原生 subagent 证据。

## 执行 subagent

1. 读 [references/usage.md](references/usage.md)，保持主 agent 的权限、范围、
   审批、网络代理和环境策略。
2. 将任务写入 UTF-8 文件，直接运行脚本，不再委派：

   ```bash
   python3 <skill-dir>/scripts/run_dsh.py \
     --config "$HOME/.dsh-codex/bridge.json" \
     --cwd /absolute/task/workspace \
     --task-file /absolute/private-task.txt
   ```

   可选 `--profile NAME`、重复有序的 `--patch /absolute/file.yml`、
   `--session-id ID`、显式 `--timeout SECONDS` 和 `--status-file ABSOLUTE_NEW_PATH`。
   默认 profile 与 `default_patches` 来自 bridge；本次覆盖不写回 profile。
3. 返回完整结构化结果及必要摘要，不输出 `.env`、完整环境、认证头或密钥。
   恢复 session 时保持相同 DSH_HOME、cwd、profile 和有序 patches；不自动重跑
   未知状态的模型请求，不自行代写目标产物。
