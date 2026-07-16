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

安装器会校验 TOML、启用 Hooks 功能，并幂等合并带标记的配置块。首次修改已有配置前，
会创建 `config.toml.fable-mode.bak`。

卸载配置：

```powershell
py -3 install_codex.py --uninstall
```

安装或升级后需重启 Codex，并使用 `/hooks` 审查和信任命令。命令内容变化后可能需要
重新确认信任。

## 事件映射

| 功能 | Codex 事件 | 当前约束 |
|---|---|---|
| Profile Injector | `SessionStart` | 完整支持，向会话注入项目状态。 |
| Delegation Guard | `SubagentStart` | 当前事件不能取消内置子代理，因此是启动后的设计门禁提示。 |
| Fail-Streak Reminder | `PostToolUse` + `Bash` | 完整支持，每连续三次失败注入归因提示。 |
| Close Guard | `Stop` | 完整支持，有未完成卡片或缺少证据时继续当前回合。 |

所有 Hooks 仅在从当前目录向上找到 `.fable/` 时生效，并在异常时 fail-open。
