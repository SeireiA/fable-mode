# 上游跟踪记录

本文档记录本仓库与上游
[`cozytab/fable5-mode`](https://github.com/cozytab/fable5-mode) 的核对基线、检查方法和后续
适配记录。每次检查上游后，应在“检查记录”中追加结果；只有完成差异评估后，才能更新基线。

## 仓库关系

| 项目 | 地址 | 默认分支 |
| --- | --- | --- |
| 本仓库 | `https://github.com/SeireiA/fable-mode.git` | `main` |
| 上游仓库 | `https://github.com/cozytab/fable5-mode.git` | `main` |

本仓库由上游内容迁移并进行 Codex 适配，但双方 Git 历史没有共同祖先。因此：

- 不使用本仓库 `HEAD` 与上游 `main` 的 ahead/behind 数字判断上游是否更新；
- 不直接合并上游分支；
- 使用“上次已核对的上游提交”与“上游当前提交”比较新增内容，再按 Codex 兼容性逐项移植。

## 当前基线

最近核对时间：`2026-07-22T16:49:31+08:00`

| 基线 | 提交 | 提交时间（北京时间） | 说明 |
| --- | --- | --- | --- |
| 已核对上游 | `893b772fb5d62f2396153ad9d2ec8a62c733def5` | `2026-07-15T14:00:22+08:00` | 上游 `main` 当前提交 |
| 本地初始适配 | `0828c906ab4c91c47cd9579cca17808319016911` | `2026-07-16T10:53:44+08:00` | 建立 Codex 适配仓库 |
| 本仓库 `main` | `7e443898c611525355694ac8011559c54318476d` | `2026-07-16T15:44:19+08:00` | 本次核对时的 `origin/main` |

上游基线提交：

```text
893b772 feat: multitasking rule in both tiers + TIER ledger directive + unified one-word controls
```

内容核对确认，本地初始适配已经包含该上游提交中的 `multitasking`、`ROUTING` 和 `TIER`
关键内容；初始适配提交中的 `README.md` 和 `README.zh-CN.md` 也与该上游提交使用相同的
Git blob。此结论仅表示本地基于该上游版本进行了适配，不表示 Claude Hooks 与 Codex Hooks
在平台能力上完全等价。

### 已确认的能力基线

| 上游能力 | 本地对应位置 | 状态 |
| --- | --- | --- |
| 两种并发层级均执行 multitasking 规则 | `SKILL.md`、`hooks/fable_profile_inject.py` | 已按 Codex 行为适配 |
| `ROUTING: quality|balanced|frugal` | `SKILL.md`、`hooks/_fable_common.py`、`fable_runner/` | 已按 Codex 模型路由适配 |
| `TIER: throughput|conservative` | `SKILL.md`、`hooks/_fable_common.py`、`fable_runner/` | 已按 Codex 调度能力适配 |

## 后续检查方法

当前仓库默认只配置 `origin`。可先直接读取上游远程引用；如果返回的提交仍为当前基线，则无需
继续比较：

```powershell
git ls-remote --symref https://github.com/cozytab/fable5-mode.git HEAD refs/heads/main
```

需要查看具体差异时，可增加只用于获取和比较的 `upstream` 远程：

```powershell
git remote add upstream https://github.com/cozytab/fable5-mode.git
git fetch upstream --prune
```

如果已存在 `upstream`，只需执行：

```powershell
git fetch upstream --prune
git show -s --date=iso-strict --format="%H%n%ad%n%an%n%s" upstream/main
```

以上一条已核对上游提交为起点检查新增提交和文件变化：

```powershell
git log --oneline --decorate 893b772fb5d62f2396153ad9d2ec8a62c733def5..upstream/main
git diff --stat 893b772fb5d62f2396153ad9d2ec8a62c733def5..upstream/main
git diff --name-status 893b772fb5d62f2396153ad9d2ec8a62c733def5..upstream/main
```

如果三条命令均无新增提交或差异，说明上游自当前基线后没有代码更新。不要使用下面的命令
作为更新判断依据，因为双方历史不相连：

```powershell
git rev-list --left-right --count HEAD...upstream/main
```

## 差异处理原则

发现上游更新后，按以下顺序处理：

1. 记录上游新提交、涉及文件和行为变化。
2. 区分通用工作协议、Claude 专属 Hook 行为和可移植实现。
3. 对 `SKILL.md`、`templates/`、`hooks/` 和安装逻辑分别评估，保持现有 Codex 接口兼容。
4. 以小范围提交移植需要的变化，不直接合并无共同历史的上游分支。
5. 运行相关测试，并记录未支持能力、替代方案和兼容性影响。
6. 完成评估后更新“当前基线”，并在下方追加检查记录。

## 检查记录

### 2026-07-22

- 检查时间：`2026-07-22T16:49:31+08:00`
- 上游 `main`：`893b772fb5d62f2396153ad9d2ec8a62c733def5`
- 自本地初始适配后的上游新提交：`0`
- 结论：上游没有新的代码更新，无需移植。
- 说明：通过远程引用、提交时间和文件内容核对；未使用无共同祖先仓库之间的 ahead/behind
  数字作结论。

### 记录模板

```text
### YYYY-MM-DD

- 检查时间：YYYY-MM-DDTHH:mm:ss+08:00
- 上游 main：<完整提交 SHA>
- 相对上一基线的新提交：<数量>
- 影响文件：<文件列表或无>
- 兼容性评估：<Codex 可直接采用 / 需要适配 / 不支持及原因>
- 处理决定：<已移植 / 暂不移植 / 无需处理>
- 本地提交：<完整提交 SHA 或无>
- 验证结果：<测试命令和结果>
```
