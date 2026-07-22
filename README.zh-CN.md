# 适用于 Codex 的 fable-mode

一套受 Fable 5 工作方式启发、面向 Codex CLI 与 Codex 桌面版的工作纪律 skill 和
Hooks。

本仓库基于
[`cozytab/fable5-mode`](https://github.com/cozytab/fable5-mode) 修改而来。上游仓库的
Hook 配置格式与 Codex Hooks 机制不兼容，本仓库在保留核心工作流程的基础上，完成了
安装器、事件映射、命令格式和守卫行为的 Codex 适配。

## 提供的能力

- 在大规模实现或委派前设置计划门禁。
- 使用 `.fable/LEDGER.md` 记录小而可验证的工作卡片。
- 会话启动时恢复当前任务上下文。
- 连续命令失败时提示进行归因分析。
- 结束任务前检查完成证据。
- 按项目选择启用：没有 `.fable/` 目录时，Hooks 保持静默。
- 可选使用 `fable_runner`，在启动子进程前校验账本、显式选择模型并调度并行卡片。

完整工作协议见 [`SKILL.md`](SKILL.md)，Hooks 机制见
[`hooks/README.md`](hooks/README.md)。

## 安装

前置要求：Git、Python 3，以及 Codex CLI 或 Codex 桌面版。

将仓库克隆到 Codex 共用的 skills 目录。Windows CMD 不会展开
`$HOME`，必须使用 `%USERPROFILE%`：

```bat
git clone https://github.com/SeireiA/fable-mode.git "%USERPROFILE%\.codex\skills\fable-mode"
cd /d "%USERPROFILE%\.codex\skills\fable-mode"
py -3 install_codex.py
```

Windows PowerShell：

```powershell
git clone https://github.com/SeireiA/fable-mode.git "$HOME/.codex/skills/fable-mode"
Set-Location "$HOME/.codex/skills/fable-mode"
py -3 install_codex.py
```

Git Bash（Windows）：

```bash
git clone https://github.com/SeireiA/fable-mode.git "$HOME/.codex/skills/fable-mode"
cd "$HOME/.codex/skills/fable-mode"
py -3 install_codex.py
```

macOS 或 Linux：

```bash
git clone https://github.com/SeireiA/fable-mode.git "$HOME/.codex/skills/fable-mode"
cd "$HOME/.codex/skills/fable-mode"
python3 install_codex.py
```

如果设置了 `CODEX_HOME`，请替换上述默认目录：CMD 使用
`%CODEX_HOME%`，PowerShell 使用 `$env:CODEX_HOME`，Git Bash、macOS 和
Linux 使用 `$CODEX_HOME`。

如果目标目录已经存在，请勿重复执行 `git clone`。通过 Git 安装的旧版请按
“更新与卸载”章节升级；目录中没有 `.git` 的旧版应先备份原目录，再重新克隆。

安装器会：

- 在 `$CODEX_HOME/config.toml` 中启用 Codex Hooks；
- 幂等写入带边界标记的配置块，不覆盖其他设置；
- 同时生成 Windows 与其他平台适用的 Python 命令；
- 每次实际修改前刷新 `config.toml.fable-mode.bak` 备份，并使用同目录原子替换写入。

安装后重启 Codex，并使用 `/hooks` 审查和信任新增命令。仓库升级后应重新运行安装器。

### 可选严格 runner

默认安装不会改变 Codex 的原生多代理行为。需要严格编排时，显式生成
`fable-strict` profile：

```powershell
py -3 install_codex.py --with-strict-runner
codex -p fable-strict
```

该 profile 位于 `$CODEX_HOME/fable-strict.config.toml`，将原生
`multi_agent` 设为 `false`，并把 skill 根目录追加到子进程的 `PYTHONPATH`。它不会自动
执行工作流；工作卡片仍由下述 `fable_runner` 命令启动。重复安装是幂等的，安装器也不会
覆盖同名但不带 fable-mode 管理标记的 profile。

## 在项目中启用

对于需要严格执行的任务，创建项目状态目录和账本：

```powershell
New-Item -ItemType Directory .fable -Force
Copy-Item "$HOME/.codex/skills/fable-mode/templates/LEDGER.template.md" ".fable/LEDGER.md"
```

Hooks 会从当前工作目录向上查找 `.fable/`，并在 Git 根目录停止。找不到该目录时，所有
Hooks 都会静默放行。

也可以直接要求 Codex 使用 fable-mode 或严谨模式来激活 skill。具体激活规则和流程见
[`SKILL.md`](SKILL.md)。

## 严格编排

`fable_runner` 是零第三方依赖的可选执行通道。它要求 Git 仓库中存在未暂停且至少有一张
开放卡片的 `.fable/LEDGER.md`，并在任何 Codex 卡片进程启动前完成清单、依赖图、路径、
worktree 和模型目录检查。可从 [`templates/workflow.example.json`](templates/workflow.example.json)
复制 schema v1 示例到 `.fable/workflow.json`。

在 `codex -p fable-strict` 会话的项目根目录运行以下命令。profile 已配置模块搜索路径，
因此业务仓库无需手工设置 `PYTHONPATH`；本仓库也可直接运行。

```powershell
py -3 -m fable_runner run --manifest .fable/workflow.json
py -3 -m fable_runner status --run-id <id> --json
py -3 -m fable_runner resume --run-id <id>
py -3 -m fable_runner cancel --run-id <id>
```

运行状态和状态迁移历史原子写入 `.fable/runs/<run-id>/`。`status` 查看已保存状态，
`resume` 继续未完成运行并将中断时仍为 `running` 的卡片重置为待调度，`cancel` 只在
进程创建身份仍匹配时终止记录的 Codex 进程树，并取消未结束卡片。后三个命令必须从该
运行所属的 Git 仓库内执行。
运行目录默认只持久化执行状态、退出码、Codex 事件类型和 thread id 等恢复元数据；
Codex JSONL 正文、stderr 以及验收 stdout/stderr 不落盘。失败输出只在当前 runner
进程内用于下一次修复提示。`.fable/` 仍是本地运行状态，不应提交或对外分享。

### Workflow manifest

| 字段 | 要求 |
|---|---|
| `schema_version` | 必须为 `1`。 |
| `models` | 必须且只能包含非空的 `lead`、`fast`、`economy` 模型标识。 |
| `timeout_seconds` | 可选；每次 Codex 调用和验收命令的超时，默认 `1800`，范围 `1..86400`。 |
| `tasks` | 非空卡片数组；卡片 ID 必须唯一，依赖必须存在且不能成环。 |
| `tasks[].id` | 1～64 字符 ASCII slug，只允许字母、数字、点、下划线和连字符。 |
| `tasks[].role` | `explorer`、`worker` 或 `verifier`。只有 `worker` 获得 `workspace-write`。 |
| `tasks[].prompt_file` | 相对 Git 根目录的 UTF-8 提示文件，必须存在且不能越出仓库。 |
| `tasks[].workspace` | 相对 Git 根目录的已有目录，必须位于已存在的 Git worktree 中。 |
| `tasks[].depends_on` | 依赖卡片 ID 数组；可为空。 |
| `tasks[].acceptance_argv` | 非空 argv 数组；runner 以 `shell=False` 在卡片 workspace 中执行。 |

`ROUTING` 和 `TIER` 不写在 manifest 中，以 `.fable/LEDGER.md` 为唯一来源。未指定时分别
使用 `balanced` 和 `conservative`。

| `ROUTING` | Explorer | Worker | Verifier |
|---|---|---|---|
| `quality` | `lead` | `lead` | `lead` |
| `balanced` | `fast` | `lead` | `lead` |
| `frugal` | `economy` | `fast` | `lead` |

`conservative` 最多运行 5 张卡片；`throughput` 使用
`max(1, min(16, CPU-2))`，CPU 数量不可用时回退为 5。
同一 Git worktree 中的多个只读卡片可以并行；写卡片会与同一 worktree 内的其他读写卡片
串行。只有位于不同、已存在 worktree 的写卡片才能并行，runner 不负责创建、合并或删除
worktree。依赖失败时，下游卡片标记为 `skipped`。

runner 对每张卡片都在外部执行 `acceptance_argv`，不采信模型自报成功。首次验收失败会
恢复同一 Codex thread；低于 `lead` 的模型连续失败后最多再提升到 `lead` 一次，不会静默
替换模型。速率限制只进行两次有界退避；耗尽后与模型不可用、鉴权失败、JSONL 异常和
超时一样使卡片明确失败。
模板中的模型名只是示例，必须按当前账户的 Codex 模型目录与权限调整。并发和重试会增加
令牌消耗，也更容易触发额度或速率限制。

只读能力探针会检查 Codex feature、模型目录、exec 参数及本地 app-server schema：

```powershell
py -3 scripts/probe_codex_capabilities.py
py -3 scripts/probe_codex_capabilities.py --run-state .fable/runs/<run-id>/run.json
```

第二种形式还会从 allowlisted `turn.completed` 元数据记录实际模型，并从状态迁移历史计算
峰值并发。未提供真实运行状态或 JSONL 未暴露模型时，对应值为 `null` 并附带原因。

### 门禁边界

Codex 原生 `SubagentStart` 在子代理启动后才触发，当前 `PreToolUse` 也不能返回启动前的
硬阻断决定，因此原生 Hooks 只能提示和强化流程。启动前账本校验、精确模型路由及禁用原生
多代理只适用于通过 `fable_runner` 启动的子进程。runner 会设置
`FABLE_ORCHESTRATOR_CHILD=1`，让这些子进程绕过父账本的 Stop/委派提示，同时用
`--disable multi_agent` 禁止再次委派。

严格 runner 是防止正常工作流意外偏离的工程门禁，不是针对恶意进程、被篡改环境变量或
直接绕过 runner 的安全边界。

## Codex Hook 映射

| 功能 | Codex 事件 | 行为 |
|---|---|---|
| Profile Injector | `SessionStart` | 注入所选配置和当前账本上下文。 |
| Delegation Guard | `SubagentStart` | 子代理启动后注入设计门禁提示。该事件不能取消内置子代理，因此此守卫属于提示性门禁。 |
| Fail-Streak Reminder | `PostToolUse` + `Bash` | 每连续三次命令失败后注入归因提示。 |
| Close Guard | `Stop` | 存在未完成卡片或已完成卡片缺少证据时继续当前回合。 |

所有 Hooks 都采用 fail-open 策略：Hook 内部异常不会阻塞 Codex 会话。

## 更新与卸载

更新 skill 并刷新 Hook 路径。Windows CMD：

```bat
cd /d "%USERPROFILE%\.codex\skills\fable-mode"
git pull
py -3 install_codex.py
```

PowerShell 或 Git Bash：

```powershell
cd "$HOME/.codex/skills/fable-mode"
git pull
py -3 install_codex.py
```

移除已注册的 Hooks：

```powershell
py -3 install_codex.py --uninstall
```

卸载器只移除带标记的 fable-mode 配置块和受管理的 strict profile，不会删除 skill 目录、
同名非受管理 profile 或其他 Codex 设置。

## 测试

实现仅使用 Python 标准库：

```powershell
py -3 tests/test_codex.py
py -3 tests/test_guards.py
py -3 tests/test_inject.py
py -3 tests/test_runner_manifest.py
py -3 tests/test_runner_executor.py
py -3 tests/test_runner_scheduler.py
py -3 tests/test_runner_recovery.py
py -3 tests/test_runner_cli.py
py -3 tests/test_probe_codex_capabilities.py
py -3 scripts/probe_codex_capabilities.py
```

## 上游与许可证

本项目基于 [`cozytab/fable5-mode`](https://github.com/cozytab/fable5-mode)，并在本仓库
维护 Codex Hooks 兼容性修改。上游核对基线和后续检查方法见
[`UPSTREAM_TRACKING.md`](UPSTREAM_TRACKING.md)。

[MIT](LICENSE) (c) 2026 cozytab 及贡献者。
