# fable-mode 的 Codex 适配

本仓库原版面向 Claude Code。Codex 适配把 skill 安装到共享的
`$CODEX_HOME/skills/fable-mode`（默认 `~/.codex/skills/fable-mode`），因此 Codex
CLI 与 Codex 桌面版会使用同一份 skill 和 hooks 配置。

## 安装 hooks

在 skill 目录运行：

```powershell
py -3 install_codex.py
```

安装器会校验并幂等合并 `$CODEX_HOME/config.toml`，首次修改前备份为
`config.toml.fable-mode.bak`。卸载 hooks：

```powershell
py -3 install_codex.py --uninstall
```

重启 Codex CLI 和桌面版后，使用 `/hooks` 审查并信任新增命令。Codex 会按命令内容的
哈希记录信任；脚本升级后需要重新审查。

## Codex hook 映射

| 功能 | Codex 事件 | 约束 |
|---|---|---|
| Profile Injector | `SessionStart` | 完整支持，向当前会话注入项目状态。 |
| Delegation Guard | `SubagentStart` | Codex 当前不能在此事件取消内置子代理，因此是启动后的设计门禁提示，不是硬拦截。 |
| Fail-Streak Reminder | `PostToolUse` / `Bash` | 完整支持，每三次连续失败注入归因提示。 |
| Close Guard | `Stop` | 完整支持，有未完成卡片或缺少证据时继续当前回合。 |

所有 hooks 只有从当前目录向上找到 `.fable/` 时才生效，并在异常时 fail-open。
