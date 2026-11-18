# rain-guard

下班前环境巡检。每日 17:30 由 launchd（`com.rain.guard`）跑一次，覆盖七块：`~/rain` 顶层结构、home 顶层结构、暂存目录滞留、用户级 LaunchAgents 与第三方 LaunchDaemons、正式项目（`apps/`）git 同步状态、赛马项目（`racing/`）静默活跃度、本地补丁（`patches/`）长期未动复核。有异常才推送：走统一推送网关的 `todo` 类，先到 iPhone 通知栏，1 分钟没处理再补一条微信；正常静默，同一组异常不重复推送。

代码仓库：`https://github.com/rainwang628/rain-guard`（公开）。换机后先按本文件部署，首次运行会自动建立新的 state 基线。

## 检查项

| 范围 | 判定 | 异常示例 |
|---|---|---|
| `~/rain` 顶层 | 可见条目（`.` 开头跳过）与 `whitelist` 完全一致 | 宿主应用静默新建目录、白名单条目被删 |
| home 顶层 | 可见条目与 `homeWhitelist` 完全一致 | 新装应用在 `~` 下自建目录（如旧 `~/WorkBuddy`）、白名单条目被删 |
| 暂存目录 | `stagingDirs.dirs` 里非隐藏、非 `ignore` 的条目 mtime 超过 `graceHours` 即报 | Downloads 里隔夜未处理的安装包、Desktop 上留了几天的截图、Documents 里非 Adobe 的遗留 |
| `~/Library/LaunchAgents` | 每个 Label 达到 `expected` 要求的水位：`present` 仅需 plist 在位 / `loaded` 需已注册到 gui 或 user 域 / `run` 需有 PID | rain 服务掉线、未登记的新 agent 出现、plist 被删 |
| `/Library/LaunchDaemons` | 同上，状态取自 system 域 | Clash 特权助手未运行、第三方 daemon 新增未登记 |
| `apps/`（正式项目） | git 仓库逐个查：未提交改动、`@{upstream}..HEAD` 未推送、未设 upstream、末次提交超 `staleDays`；非 git 仓库单列提示 | 有提交没 push、分支没跟踪远端、太久没提交、正式项目还没纳管 |
| `racing/`（赛马项目） | 取 git 末次提交与目录文件 mtime 的较新者算静默天数：`≥staleDays` 列可清理候选，`≤activeDays` 列近期活跃可跟进 | 赛马项目长期不动该清理、最近有动静可跟进 |
| `patches/`（本地补丁） | 按目录文件 mtime 算静默天数：`≥staleDays` 列长期未动复核候选；不查 git 同步（这类本就不建仓库） | 本地补丁长期没动，复核是否仍需要 |

水位按需配置，避免把「人为禁用」误报成故障：`com.google.GoogleUpdater.wake`、`com.valvesoftware.steamclean`、`io.github.clash-verge-rev.clash-verge-rev` 只要求 `present`，因为它们是主动关掉的。

### 配置段

| 段 | 默认 | 说明 |
|---|---|---|
| `whitelist` | rain 顶层 7 项 | 与 `AGENTS.macos.md` 硬性禁令的清单保持一致 |
| `homeWhitelist` | home 顶层 11 项可见目录 | **已知基线，不是「要搬走」**：系统类、`jiaojiao` 照样登记，只是不告警。新增应用目录才报 |
| `stagingDirs.dirs` | `~/rain/tmp/inbox` | 浏览器默认下载路径改到 `~/rain/tmp/inbox/`；`~/Downloads`／`~/Documents`／`~/Desktop` 全部移出巡检（三者均受 TCC 保护，需 FDA 才能读） |
| `stagingDirs.graceHours` | `24` | 宽限期，刚下载的文件不报；设 `0` 则出现即报 |
| `stagingDirs.ignore` | `.DS_Store`、`.localized` | 隐藏项本就跳过；这里只列需要额外忽略的名字 |
| `formalProjects` | `staleDays: 3`，`dir: ~/rain/projs/apps` | 正式项目巡检；`enabled: false` 停用 |
| `racingProjects` | `staleDays: 30`，`activeDays: 7`，`dir: ~/rain/projs/racing` | 赛马项目巡检；两档之间（8–29 天）不报，避免中间态天天打扰 |
| `patchProjects` | `staleDays: 60`，`dir: ~/rain/projs/patches` | 本地补丁巡检；阈值比赛马长，只报长期未动的复核候选 |

`homeWhitelist`、`stagingDirs` 不配就不查对应项。三段项目巡检默认启用（内建默认目录 `~/rain/projs/apps`、`~/rain/projs/racing` 与 `~/rain/projs/patches`），要停用需显式写 `enabled: false`。项目巡检忽略 `node_modules`、`.venv`、`dist` 等构建目录，只按源码文件的 mtime 算活跃度。

演练项目巡检可用 `RAIN_APPS_DIR`、`RAIN_RACING_DIR`、`RAIN_PATCH_DIR` 指向假目录，不碰真实项目。

## 下载落点与权限边界

**约定：浏览器下载路径改到 `~/rain/tmp/inbox/`，不在 `~/Downloads` 落任何东西。** 暂存巡检只查这一个目录，全在 `~/rain` 内完成——`~/rain` 不在 macOS TCC 保护清单里，无需 FDA 授权。`~/Downloads`、`~/Documents`、`~/Desktop` 三者都受 TCC 保护，全部移出巡检；原先给 `/usr/bin/python3` 和 WorkBuddy 加的完全磁盘访问可以撤回。

- Safari：设置 → 通用 → 文件下载位置 → 选 `~/rain/tmp/inbox/`
- Chrome：设置 → 下载内容 → 位置 → 选 `~/rain/tmp/inbox/`
- AirDrop、微信／QQ 收文件、桌面截图等无法改路径的场景，仍会落到受保护目录；这类残留只能人工偶尔清理，不进自动巡检。

目录读取被拒绝时脚本不静默跳过，而是记一条「巡检受限」异常并照常推送一次（去重后不重复打扰）。若返回空列表但 Finder 里可见文件，属权限导致的假空。launchd 启动的脚本不受 WorkBuddy / DSH 会话沙箱约束；正常配置下不需要 FDA，本仓库不负责申请授权。

## 背景

`~/rain` 顶层是核心结构：新增顶层目录须先报告确认（`AGENTS.macos.md` 硬性禁令，规则 V7.3 起）。宿主应用（DSH、WorkBuddy 等）会按自己的固定路径静默建目录，不看规则；launchd 侧则会出现被静默 bootout、被系统后台项拦截、服务崩溃后无人发现的情况。本服务负责在没人看的时候发现并告知。V1.2 把同一套 diff 机制扩到 home 顶层，兜住「新装应用在 `~` 下自建目录」，并按「暂存目录不留文件」的既有要求检查滞留文件。暂存巡检从 TCC 保护的 `~/Downloads`／`~/Documents` 迁到 `~/rain/tmp/inbox/`，把「下载即暂存、超时提醒」的治理留在无需系统授权的路径内。V1.3 把同一套巡检扩到项目成熟度：`apps/` 正式项目盯 git 同步，`racing/` 赛马项目盯静默活跃度，让「该提交没提交」和「赛马该清理」在下班前一次报出来。V1.4 新增 `patches/` 本地补丁一组，按更长的静默阈值提示长期未动的复核候选，不查 git 同步（这类不建仓库）。V1.5 把推送出口从直连 PushPlus 改成统一网关 `push-gateway`（`todo` 类，凭据只留在网关一处），并让 `gatewayScript` 支持 `~` 展开。 V1.6 把 `apps/` 的纳管要求收紧：非 git、没有远端都直接报不合格（合格项见 `~/rain/projs/README.md` 的「新增或迁移项目」后校验）。V1.7 优化通知排版：只列异常分块并按紧急度排序，同类长串逐项展开，正常项折叠为一行计数；异常集合与去重逻辑不变。

## 文件

| 文件 | 说明 |
|---|---|
| `guard.py` | 巡检脚本；`--dry-run` 只打印不推送，`--test-push` 发测试消息 |
| `config.example.json` | 配置模板；首次部署时 `cp config.example.json config.json` 再按本机补基线 |
| `com.rain.guard.plist` | launchd 定义（每日 17:30），已复制到 `~/Library/LaunchAgents/` |
| `state.json` | 上次的异常集合，防重复打扰；也用于判断「已恢复」 |
| `guard.log`、`launchd.*.log` | 日志 |

## 手动使用

```bash
python3 guard.py --dry-run                            # 看七块结果，不推送
python3 guard.py --test-push                          # 验证推送通道（会真发一条 todo）
RAIN_ROOT=/tmp/fake-root RAIN_HOME_ROOT=/tmp/fake-home \
  RAIN_STAGING_DIRS=/tmp/fake-downloads,/tmp/fake-desktop \
  RAIN_APPS_DIR=/tmp/fake-apps RAIN_RACING_DIR=/tmp/fake-racing \
  python3 guard.py --dry-run                          # 演练异常（全假路径，不碰真实目录）
```

可用环境变量覆盖，便于演练：`RAIN_ROOT`、`RAIN_HOME_ROOT`、`RAIN_STAGING_DIRS`（逗号分隔）、`RAIN_APPS_DIR`、`RAIN_RACING_DIR`、`RAIN_AGENT_DIR`、`RAIN_DAEMON_DIR`、`RAIN_CONFIG`、`RAIN_STATE_PATH`。演练时另配 `RAIN_CONFIG` 指向只含白名单段的假配置，可跳过 launchd 检查。

**验证真实权限时不要用会话内 `--dry-run`**：宿主会话派生的 python3 的 TCC 归属是宿主进程，会假报「巡检受限」。让 launchd 自己跑再读结果：`launchctl kickstart -k gui/$(id -u)/com.rain.guard`，然后看 `guard.log` 与 `state.json`。

## 部署与回滚

- 源码在 `~/rain/projs/apps/rain-guard`，部署根在 `~/rain/projs/services/rain-guard`；改代码只改源码，再 `./deploy.sh rain-guard` 单向推送，**禁止在部署根手改代码**。
- 部署：`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.rain.guard.plist`
- 回滚：`launchctl bootout gui/$(id -u)/com.rain.guard && rm ~/Library/LaunchAgents/com.rain.guard.plist`
- 基线变更：rain 顶层白名单改 `config.json` 并同步 `AGENTS.macos.md`；home 白名单与暂存目录改 `config.json` 的 `homeWhitelist` / `stagingDirs`；launchd 基线改 `launchdAgents` / `launchdDaemons`，新增服务时把对应 Label 加进去，否则会报「新增未登记」；项目巡检阈值改 `formalProjects` / `racingProjects` / `patchProjects`。
- 改脚本或 `config.json` 无需重载 plist；改 plist 的触发时间需 `bootout` + `bootstrap`。V1.3 未改 plist，部署后原有的 17:30 任务继续执行新脚本。
