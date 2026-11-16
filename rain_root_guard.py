#!/usr/bin/env python3
# 文件版本：V1.2｜更新日期：2026-11-16
# 下班前环境巡检：rain / home 顶层结构 + 暂存目录滞留 + LaunchAgents / LaunchDaemons 期望状态。有异常才推送，无异常静默。
"""巡检四块：
1. ~/rain 顶层可见条目是否偏离 whitelist；
2. home 顶层可见条目是否偏离 homeWhitelist（兜住宿主应用静默建目录）；
3. Downloads / Desktop 等暂存目录是否有超期滞留文件；
4. 关键 launchd 任务是否在位、已加载、在运行（用户级 LaunchAgents 与第三方 LaunchDaemons）。
任一异常走 PushPlus 推送；与上次异常集合相同则不重复打扰。手动调试见 README。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("RAIN_CONFIG", str(HERE / "config.json")))
STATE_PATH = Path(os.environ.get("RAIN_STATE_PATH", str(HERE / "state.json")))
LOG_PATH = HERE / "guard.log"

ROOT = Path(os.environ.get("RAIN_ROOT", str(Path.home() / "rain")))
HOME_ROOT = Path(os.environ.get("RAIN_HOME_ROOT", str(Path.home())))
UID = str(os.getuid())

LEVELS = {"present": 0, "loaded": 1, "run": 2}
LEVEL_TEXT = {"present": "仅需在位", "loaded": "需已加载", "run": "需运行中"}


def log(line: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {line}\n")


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def resolve_token(config: dict) -> str:
    token = (config.get("pushplusToken") or "").strip()
    if token:
        return token
    ref = config.get("pushplusConfigPath")
    if ref:
        other = load_json(Path(os.path.expanduser(ref)), {})
        return (other.get("pushplusToken") or "").strip()
    return ""


def push(config: dict, title: str, content: str) -> tuple[bool, str]:
    token = resolve_token(config)
    if not token:
        return False, "pushplusToken 未配置"
    payload = json.dumps(
        {"token": token, "title": title, "content": content, "template": "txt"}
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://www.pushplus.plus/send",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8", "replace")
            return response.status == 200 and '"code":200' in body.replace(" ", ""), body
    except OSError as exc:
        return False, str(exc)


def save_state(issues: list[str]) -> None:
    STATE_PATH.write_text(
        json.dumps({"issues": issues, "updated": datetime.now().isoformat(timespec="seconds")},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


def describe(path: Path) -> str:
    if path.is_symlink():
        return "软链"
    if path.is_dir():
        return "目录"
    return "文件"


# ---------- 检查一：目录结构与白名单 ----------

def check_root(whitelist: set[str], root: Path | None = None,
               label: str = "~/rain") -> list[str]:
    target = root or ROOT
    if not whitelist:
        return [f"config 缺少 {label} 白名单"]
    try:
        entries = {
            item.name: describe(item)
            for item in target.iterdir()
            if not item.name.startswith(".")
        }
    except OSError as exc:
        return [f"扫描 {target} 失败：{exc}"]

    issues = []
    unexpected = sorted(n for n in entries if n not in whitelist)
    missing = sorted(n for n in whitelist if n not in entries)
    if unexpected:
        issues.append(f"{label} 未登记条目：" + "；".join(f"{n}（{entries[n]}）" for n in unexpected))
    if missing:
        issues.append(f"{label} 白名单缺失：" + "、".join(missing))
    return issues


# ---------- 检查二：暂存目录滞留 ----------

def check_staging(block: dict) -> list[str]:
    """Downloads / Desktop 等只作暂存，超期未清理的非隐藏条目才报。"""
    dirs = block.get("dirs") or []
    if not dirs:
        return []
    override = os.environ.get("RAIN_STAGING_DIRS")
    if override:
        dirs = [p for p in override.split(",") if p]
    grace_hours = float(block.get("graceHours", 24))
    ignore = set(block.get("ignore") or [])
    now = datetime.now().timestamp()
    cutoff = now - grace_hours * 3600
    issues: list[str] = []
    for raw in dirs:
        path = Path(os.path.expanduser(raw))
        try:
            entries = [
                item for item in path.iterdir()
                if item.name not in ignore and not item.name.startswith(".")
            ]
        except FileNotFoundError:
            issues.append(f"{raw} 目录不存在")
            continue
        except PermissionError:
            issues.append(
                f"{raw} 巡检受限（权限拒绝）：需在系统设置 → 隐私与安全性 → 完全磁盘访问授权"
            )
            continue
        except OSError as exc:
            issues.append(f"{raw} 巡检受限（{type(exc).__name__}）：{exc}")
            continue
        stale = []
        for item in sorted(entries, key=lambda p: p.name):
            try:
                mtime = item.stat().st_mtime
            except OSError:
                continue
            if mtime <= cutoff:
                stale.append(f"{item.name}（滞留 {int((now - mtime) // 3600)}h）")
        if stale:
            suffix = "…" if len(stale) > 8 else ""
            issues.append(
                f"{raw} 有 {len(stale)} 项滞留超过 {int(grace_hours)}h："
                + "、".join(stale[:8]) + suffix
            )
    return issues


# ---------- 检查三：launchd 期望状态 ----------

def loaded_services(domains: tuple[str, ...]) -> dict[str, int]:
    """读取指定域的服务表，返回 label -> pid（0 表示已加载未运行）。"""
    merged: dict[str, int] = {}
    for domain in domains:
        try:
            out = subprocess.run(
                ["launchctl", "print", domain],
                capture_output=True,
                text=True,
                timeout=20,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        match = re.search(r"\tservices = \{(.*?)\n\t\}", out, re.S)
        if not match:
            continue
        for line in match.group(1).splitlines():
            row = re.match(r"^\s+(\d+)\s+(-?\d+|-)\s+(\S+)\s*$", line)
            if row:
                label, pid = row.group(3), int(row.group(1))
                merged[label] = max(merged.get(label, 0), pid)
    return merged


def plist_labels(directory: Path) -> dict[str, str]:
    """目录内 plist 文件名 -> label（读不到 Label 时用文件名兜底）。"""
    labels: dict[str, str] = {}
    if not directory.is_dir():
        return labels
    for item in sorted(directory.glob("*.plist")):
        label = item.stem
        try:
            out = subprocess.run(
                ["/usr/bin/plutil", "-convert", "json", "-o", "-", str(item)],
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout
            if out.strip():
                label = json.loads(out).get("Label") or label
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        labels[label] = item.name
    return labels


def check_launchd(kind: str, directory: Path, expected: dict[str, str],
                  services: dict[str, int]) -> list[str]:
    """kind 用于措辞（用户级 / 第三方 daemon）。"""
    present = plist_labels(directory)
    issues = []
    for label in sorted(set(present) - set(expected)):
        issues.append(f"{kind} 新增未登记：{label}（{present[label]}）")
    for label, level in sorted(expected.items()):
        if label not in present:
            issues.append(f"{kind} 缺少 plist：{label}")
            continue
        got = 2 if services.get(label, 0) > 0 else (1 if label in services else 0)
        if got < LEVELS.get(level, 0):
            state = ["未加载", "已加载未运行", "运行中"][got]
            issues.append(f"{label} 当前{state}，应为{LEVEL_TEXT.get(level, level)}")
    return issues


def main() -> int:
    config = load_json(CONFIG_PATH, {})
    if "--test-push" in sys.argv:
        ok, detail = push(config, "rain-root-guard 通道测试", "收到这条说明巡检推送通道正常。")
        print(ok, detail[:200])
        return 0 if ok else 1

    dry = "--dry-run" in sys.argv
    sections: list[tuple[str, list[str]]] = []
    sections.append(("~/rain 顶层结构", check_root(set(config.get("whitelist") or []))))

    home_wl = set(config.get("homeWhitelist") or [])
    if home_wl:
        sections.append(("home 顶层结构", check_root(home_wl, HOME_ROOT, "home 顶层")))

    staging = config.get("stagingDirs") or {}
    if staging.get("dirs"):
        sections.append(("暂存目录滞留", check_staging(staging)))

    user_services = loaded_services((f"gui/{UID}", f"user/{UID}"))
    system_services = loaded_services(("system",))
    if not user_services and not system_services:
        sections.append(("launchd", ["读取 launchctl 服务表失败（gui/user/system 域均无结果）"]))
    else:
        for key, kind, svc, env in (
            ("launchdAgents", "用户级 LaunchAgent", user_services, "RAIN_AGENT_DIR"),
            ("launchdDaemons", "第三方 LaunchDaemon", system_services, "RAIN_DAEMON_DIR"),
        ):
            block = config.get(key) or {}
            if not block:
                continue
            directory = Path(os.path.expanduser(os.environ.get(env) or block.get("dir") or ""))
            sections.append(
                (f"{kind}（{directory}）",
                 check_launchd(kind, directory, block.get("expected") or {}, svc))
            )

    issues = [line for _, block in sections for line in block]
    body = "\n".join(
        f"【{name}】\n" + ("\n".join(f"· {line}" for line in block) if block else "· 正常")
        for name, block in sections
    )
    title = f"下班前巡检：{len(issues)} 项异常" if issues else "下班前巡检：全部正常"

    if dry:
        print(f"[dry-run] {title}\n{body}")
        return 0

    prev = load_json(STATE_PATH, {})
    if "issues" not in prev:  # V1.0 旧状态只有 root 差异，按空集比较并升级格式
        prev["issues"] = []
    if sorted(issues) == sorted(prev["issues"]):
        if "updated" not in prev:
            save_state(issues)
        log("无异常" if not issues else "异常未变化，跳过重复推送")
        return 0

    if issues:
        ok, detail = push(
            config,
            title,
            body + "\n\n新增顶层目录须先报告确认；暂存目录内容按规则移进 ~/rain。",
        )
        log(f"推送{'成功' if ok else '失败'}：{title} | {detail[:200]}")
        if not ok:
            return 1
    else:
        log("已恢复正常，未推送")
    save_state(issues)
    return 0


if __name__ == "__main__":
    sys.exit(main())
