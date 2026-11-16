# rain-root-guard

下班前环境巡检。每日 17:30 由 launchd（`com.rain.rain-root-guard`）跑一次，覆盖三块：`~/rain` 顶层结构、用户级 LaunchAgents、第三方 LaunchDaemons。有异常才推送 PushPlus 微信，正常静默；同一组异常不重复推送。

## 检查项

| 范围 | 判定 | 异常示例 |
|---|---|---|
| `~/rain` 顶层 | 可见条目（`.` 开头跳过）与 `whitelist` 完全一致 | 宿主应用静默新建目录、白名单条目被删 |
| `~/Library/LaunchAgents` | 每个 Label 达到 `expected` 要求的水位：`present` 仅需 plist 在位 / `loaded` 需已注册到 gui 或 user 域 / `run` 需有 PID | rain 服务掉线、未登记的新 agent 出现、plist 被删 |
| `/Library/LaunchDaemons` | 同上，状态取自 system 域 | Clash 特权助手未运行、第三方 daemon 新增未登记 |

水位按需配置，避免把「人为禁用」误报成故障：`com.google.GoogleUpdater.wake`、`com.valvesoftware.steamclean`、`io.github.clash-verge-rev.clash-verge-rev` 只要求 `present`，因为它们是主动关掉的。

## 背景

`~/rain` 顶层是核心结构：新增顶层目录须先报告确认（`AGENTS.macos.md` 硬性禁令，规则 V7.3 起）。宿主应用（DSH、WorkBuddy 等）会按自己的固定路径静默建目录，不看规则；launchd 侧则会出现被静默 bootout、被系统后台项拦截、服务崩溃后无人发现的情况。本服务负责在没人看的时候发现并告知。

## 文件

| 文件 | 说明 |
|---|---|
| `rain_root_guard.py` | 巡检脚本；`--dry-run` 只打印不推送，`--test-push` 发测试消息 |
| `config.json` | 三块检查的基线：`whitelist`、`launchdAgents`、`launchdDaemons`；PushPlus token 从 aa-notify 的 config 引用，不重复存凭据 |
| `com.rain.rain-root-guard.plist` | launchd 定义（每日 17:30），已复制到 `~/Library/LaunchAgents/` |
| `state.json` | 上次的异常集合，防重复打扰；也用于判断「已恢复」 |
| `guard.log`、`launchd.*.log` | 日志 |

## 手动使用

```bash
python3 rain_root_guard.py --dry-run                            # 看三块结果，不推送
python3 rain_root_guard.py --test-push                          # 验证微信通道
RAIN_ROOT=/tmp/fake-root RAIN_AGENT_DIR=/tmp/fake-agents \
  RAIN_DAEMON_DIR=/tmp/fake-daemons python3 rain_root_guard.py --dry-run   # 演练异常
```

可用环境变量覆盖，便于演练：`RAIN_ROOT`、`RAIN_AGENT_DIR`、`RAIN_DAEMON_DIR`、`RAIN_CONFIG`、`RAIN_STATE_PATH`。

## 部署与回滚

- 部署：`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.rain.rain-root-guard.plist`
- 回滚：`launchctl bootout gui/$(id -u)/com.rain.rain-root-guard && rm ~/Library/LaunchAgents/com.rain.rain-root-guard.plist`
- 基线变更：顶层白名单改 `config.json` 并同步 `AGENTS.macos.md`；launchd 基线改 `config.json` 的 `launchdAgents` / `launchdDaemons`，新增服务时把对应 Label 加进去，否则会报「新增未登记」。
- 改脚本无需重载 plist；改 plist 的触发时间需 `bootout` + `bootstrap`。
