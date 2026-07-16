# 适用于 Codex 的 fable-mode

一套受 Fable 5 工作方式启发、面向 Codex CLI 与 Codex 桌面版的工作纪律 skill 和
Hooks。

本仓库基于
[`cozytab/fable5-mode`](https://github.com/cozytab/fable5-mode) 修改而来。上游仓库的
Hook 配置格式与 Codex Hooks 机制不兼容。本仓库保留核心工作流程，并完成安装器、事件
映射、命令格式和守卫行为的 Codex 适配。

## 提供的能力

- 在大规模实现或委派前设置计划门禁。
- 使用 `.fable/LEDGER.md` 记录小而可验证的工作卡片。
- 会话启动时恢复当前任务上下文。
- 连续命令失败时提示进行归因分析。
- 结束任务前检查完成证据。
- 按项目选择启用：没有 `.fable/` 目录时，Hooks 保持静默。

完整工作协议见 [`SKILL.md`](SKILL.md)，Hooks 机制见
[`hooks/README.md`](hooks/README.md)。

## 安装

前置要求：Git、Python 3，以及 Codex CLI 或 Codex 桌面版。

将仓库克隆到 Codex 共用的 skills 目录：

```powershell
git clone https://github.com/SeireiA/fable-mode.git "$HOME/.codex/skills/fable-mode"
cd "$HOME/.codex/skills/fable-mode"
py -3 install_codex.py
```

在 macOS 或 Linux 上，将 `py -3 install_codex.py` 替换为
`python3 install_codex.py`。如果设置了 `CODEX_HOME`，请安装到
`$CODEX_HOME/skills/fable-mode`。

安装器会：

- 在 `$CODEX_HOME/config.toml` 中启用 Codex Hooks；
- 幂等写入带边界标记的配置块，不覆盖其他设置；
- 同时生成 Windows 与其他平台适用的 Python 命令；
- 首次修改前创建 `config.toml.fable-mode.bak` 备份。

安装后重启 Codex，并使用 `/hooks` 审查和信任新增命令。仓库升级后应重新运行安装器。

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

## Codex Hook 映射

| 功能 | Codex 事件 | 行为 |
|---|---|---|
| Profile Injector | `SessionStart` | 注入所选配置和当前账本上下文。 |
| Delegation Guard | `SubagentStart` | 子代理启动后注入设计门禁提示。该事件不能取消内置子代理，因此此守卫属于提示性门禁。 |
| Fail-Streak Reminder | `PostToolUse` + `Bash` | 每连续三次命令失败后注入归因提示。 |
| Close Guard | `Stop` | 存在未完成卡片或已完成卡片缺少证据时继续当前回合。 |

所有 Hooks 都采用 fail-open 策略：Hook 内部异常不会阻塞 Codex 会话。

## 更新与卸载

更新 skill 并刷新 Hook 路径：

```powershell
cd "$HOME/.codex/skills/fable-mode"
git pull
py -3 install_codex.py
```

移除已注册的 Hooks：

```powershell
py -3 install_codex.py --uninstall
```

卸载器只移除带标记的 fable-mode 配置块，不会删除 skill 目录或其他 Codex 设置。

## 测试

实现仅使用 Python 标准库：

```powershell
py -3 tests/test_codex.py
py -3 tests/test_guards.py
py -3 tests/test_inject.py
```

## 上游与许可证

本项目基于 [`cozytab/fable5-mode`](https://github.com/cozytab/fable5-mode)，并在本仓库
维护 Codex Hooks 兼容性修改。

[MIT](LICENSE) (c) 2026 cozytab 及贡献者。
