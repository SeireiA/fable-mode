# 面向 Codex 的 fable-mode 守卫 Hooks

本目录包含适配 Codex 的守卫 Hooks。本仓库基于
[`cozytab/fable5-mode`](https://github.com/cozytab/fable5-mode) 修改而来，上游 Hook
配置格式与 Codex Hooks 机制不兼容。

使用以下命令安装并注册 Hooks：

```powershell
py -3 ../install_codex.py
```

仅在需要严格 runner 时生成独立 profile：

```powershell
py -3 ../install_codex.py --with-strict-runner
codex -p fable-strict
```

完整安装说明见 [`../README.md`](../README.md)，适配细节见
[`../README.codex.zh-CN.md`](../README.codex.zh-CN.md)。

## Hook 映射

| Hook | Codex 事件 | 用途 |
|---|---|---|
| `fable_profile_inject.py` | `SessionStart` | 注入所选执行层级、工作规则和账本上下文。 |
| `fable_spawn_guard.py` | `SubagentStart` | 在没有开放卡片却进行大规模委派时注入设计门禁提示。 |
| `fable_fail_streak.py` | `PostToolUse` + `Bash` | 每连续三次命令失败后提示进行失败归因。 |
| `fable_close_guard.py` | `Stop` | 存在开放卡片或完成卡片缺少证据时继续当前回合。 |

`fable_lint.py` 是一次性检查工具，不是 Hook。它检查项目规格和账本中缺失的来源标记、
验收条件与完成证据：

```powershell
py -3 fable_lint.py <项目目录>
```

## 项目级启用

脚本会从当前工作目录向上查找 `.fable/`，并以 Git 根目录为边界：

- 存在 `.fable/` 时，守卫对当前项目生效。
- 不存在 `.fable/` 时，Hooks 静默放行，不改变会话行为。

除下述 strict runner 子进程的精简上下文外，Hooks 只在找到 `.fable/` 时运行。所有脚本
都采用 fail-open 策略，脚本内部异常不会阻塞 Codex。

## 账本格式

`.fable/LEDGER.md` 是当前工作轮次的小型状态机：

```text
- [ ] 1. 开放卡片，并包含可由机器检查的验收条件
- [x] 2. 已完成卡片 -- evidence: pytest 21/21
- [~] 3. 已延期卡片 -- deferred: 原因
PAUSED: 原因
ROUTING: balanced
TIER: conservative
```

- `- [ ]` 表示开放，会阻止当前回合结束。
- `- [x]` 表示完成，并要求提供有效的证据标记。
- `- [~]` 表示已明确延期，本轮视为关闭。
- `PAUSED: 原因` 暂时停用工作流程守卫。
- `ROUTING` 可选择 `quality`、`balanced` 或 `frugal` 路由策略。
- `TIER` 可选择 `throughput` 或 `conservative` 并发层级。

`SPEC.md` 和 `PROGRESS.md` 是持久化项目文档。账本只保存当前轮次的执行状态。

## 当前限制

`SubagentStart` 事件不能取消已经启动的内置子代理。因此，委派守卫只能在启动后注入提示，
无法实现启动前的硬拦截。当前 `PreToolUse` 协议也不能返回可阻止内置委派的决定。原生
Hooks 是兼容且 fail-open 的流程强化层，不是硬委派门禁。

## Strict runner 子进程

只有 `fable_runner` 通道在启动卡片子进程前验证开放账本、manifest、模型目录、路径和依赖图。
它通过 `codex exec --disable multi_agent` 运行卡片，并设置
`FABLE_ORCHESTRATOR_CHILD=1`。守卫对该变量的处理如下：

- Profile Injector 只注入“完成当前卡片、不得再次委派”的精简上下文。
- Delegation Guard 静默放行，不用父账本再次判断子进程的委派资格。
- Close Guard 静默放行，由父 runner 负责验收、重试和运行状态。
- Fail-Streak Reminder 始终只是提示，不会阻止子进程退出。

这样可避免多个子进程争用共享 `.fable/LEDGER.md`，同时保留父 runner 的单一状态所有权。
它只约束通过 runner 正常启动的进程；环境变量可被修改、脚本可被绕过，因此不是抗恶意
绕过的安全边界。

runner 的公共命令为 `run`、`status`、`resume` 和 `cancel`。manifest 字段、模型路由矩阵、
共享 worktree 串行规则及额度风险见 [`../README.zh-CN.md`](../README.zh-CN.md)。

## 安全设计

- 未选择启用的项目不受 Hooks 影响。
- Close Guard 具备循环保护。
- Hook 异常会直接放行。
- 会话状态保存在临时目录中，并会自动过期。

## 测试

在仓库根目录运行仅依赖 Python 标准库的测试：

```powershell
py -3 tests/test_codex.py
py -3 tests/test_guards.py
py -3 tests/test_inject.py
```
