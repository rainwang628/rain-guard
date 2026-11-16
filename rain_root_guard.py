#!/usr/bin/env python3
# 文件版本：V1.1｜更新日期：2026-11-16
# 下班前环境巡检：~/rain 顶层结构 + LaunchAgents / LaunchDaemons 期望状态。有异常才推送，无异常静默。
"""巡检两件事：
1. ~/rain 顶层可见条目是否偏离白名单；
2. 关键 launchd 任务是否在位、已加载、在运行（用户级 LaunchAgents 与第三方 LaunchDaemons）。
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


# ---------- 检查一：~/rain 顶层结构 ----------

def check_root(whitelist: set[str]) -> list[str]:
    if not whitelist:
        return ["config 缺少 whitelist"]
    try:
        entries = {
            item.name: describe(item)
            for item in ROOT.iterdir()
            if not item.name.startswith(".")
        }
    except OSError as exc:
        return [f"扫描 {ROOT} 失败：{exc}"]

    issues = []
    unexpected = sorted(n for n in entries if n not in whitelist)
    missing = sorted(n for n in whitelist if n not in entries)
    if unexpected:
        issues.append("未登记条目：" + "；".join(f"{n}（{entries[n]}）" for n in unexpected))
    if missing:
        issues.append("白名单缺失：" + "、".join(missing))
    return issues


# ---------- 检查二：launchd 期望状态 ----------

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
            body + "\n\n新增顶层目录须先报告确认，见 ~/rain/projs/agent-rules/AGENTS.macos.md。",
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
