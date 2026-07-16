# fable-mode 的 Codex Hooks 适配说明

本仓库基于
[`cozytab/fable5-mode`](https://github.com/cozytab/fable5-mode) 修改而来。上游 Hook
配置格式与 Codex Hooks 机制不兼容，因此本仓库提供了 Codex 专用安装器和事件映射。

主安装与使用说明见 [`README.zh-CN.md`](README.zh-CN.md)。本文只记录适配细节和当前
限制。

## 配置位置

Codex CLI 与 Codex 桌面版共用 `$CODEX_HOME`。默认安装位置为
`~/.codex/skills/fable-mode`，Hook 配置写入 `$CODEX_HOME/config.toml`。

```powershell
py -3 install_codex.py
```

安装器会校验 TOML、启用 Hooks 功能，并幂等合并带标记的配置块。每次实际修改前会刷新
`config.toml.fable-mode.bak`，再通过同目录临时文件和原子替换写入；strict profile 与主
配置任一写入失败时会恢复本次操作前的两个文件。

默认安装保留原生多代理行为。需要使用 `fable_runner` 严格编排时，显式生成独立 profile：

```powershell
py -3 install_codex.py --with-strict-runner
codex -p fable-strict
```

安装器在 `$CODEX_HOME/fable-strict.config.toml` 中写入受管理标记、
`multi_agent = false`，并通过 `shell_environment_policy` 将 skill 根目录追加到
`PYTHONPATH`。若同名文件不是 fable-mode 管理的 profile，安装器会拒绝覆盖。启动
`codex -p fable-strict` 会关闭该会话的原生多代理并使业务仓库可导入 runner；工作流仍需
通过 `py -3 -m fable_runner run --manifest <path>` 显式启动。

卸载配置：

```powershell
py -3 install_codex.py --uninstall
```

卸载只删除带 fable-mode 标记的 Hook 配置和受管理的 strict profile，并恢复安装前的
`features.hooks` 值。用户原有配置和同名非受管理 profile 保持不变。

安装或升级后需重启 Codex，并使用 `/hooks` 审查和信任命令。命令内容变化后可能需要
重新确认信任。

## 事件映射

| 功能 | Codex 事件 | 当前约束 |
|---|---|---|
| Profile Injector | `SessionStart` | 完整支持，向会话注入项目状态。 |
| Delegation Guard | `SubagentStart` | 当前事件不能取消内置子代理，因此是启动后的设计门禁提示。 |
| Fail-Streak Reminder | `PostToolUse` + `Bash` | 完整支持，每连续三次失败注入归因提示。 |
| Close Guard | `Stop` | 完整支持，有未完成卡片或缺少证据时继续当前回合。 |

常规会话中的 Hooks 仅在从当前目录向上找到 `.fable/` 时生效，并在异常时 fail-open。
`FABLE_ORCHESTRATOR_CHILD=1` 的精简子进程上下文是下述例外。

## 原生门禁限制

`SubagentStart` 在内置子代理已经启动后才触发，不能取消该子代理。当前 Codex Hook 协议的
`PreToolUse` 也不能返回启动前的硬阻断决定。因此，原生委派守卫只能注入提醒，不能宣称
能够硬拦截内置委派；默认安装继续保持这一兼容行为。

严格的启动前账本校验只属于 `fable_runner` 通道。runner 完成 manifest、开放账本、模型
目录、依赖图、路径和 worktree 预检后，才用显式模型与 sandbox 调用 `codex exec`。每个
子进程带有 `FABLE_ORCHESTRATOR_CHILD=1`：Profile Injector 只注入精简卡片上下文，
Delegation Guard 和 Close Guard 不再用父账本阻塞该子进程；runner 同时传入
`--disable multi_agent`，禁止子进程继续原生委派。

这一环境变量和 profile 都是正常工作流的协作约定，不是针对恶意绕过的安全边界。直接
启动其他 Codex 进程、篡改环境变量或修改本地脚本仍可绕过 runner。

## Runner 接口摘要

```powershell
py -3 -m fable_runner run --manifest .fable/workflow.json
py -3 -m fable_runner status --run-id <id> --json
py -3 -m fable_runner resume --run-id <id>
py -3 -m fable_runner cancel --run-id <id>
```

能力探针默认只读取本地 CLI/schema；提供真实运行状态后还会报告实际模型元数据和峰值并发：

```powershell
py -3 scripts/probe_codex_capabilities.py --run-state .fable/runs/<run-id>/run.json
```

schema v1 的 manifest 包含 `models`、可选 `timeout_seconds` 以及 `tasks`。每张卡片需要
`id`、`role`、`prompt_file`、`workspace`、`depends_on` 和非空的
`acceptance_argv`。完整字段、路由矩阵、worktree 并发规则及模型/额度风险见
[`README.zh-CN.md`](README.zh-CN.md) 和
[`templates/workflow.example.json`](templates/workflow.example.json)。
