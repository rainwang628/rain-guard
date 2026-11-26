#!/usr/bin/env python3
# 文件版本：V1.10｜更新日期：2026-11-24
# 下班前环境巡检：rain / home 顶层结构 + 暂存目录滞留 + LaunchAgents / LaunchDaemons + 项目成熟度巡检。有异常才推送，无异常静默；每周五 17:30 那次末尾追加一条周汇总（info 类，全绿也发）。
"""巡检十一块：
1. ~/rain 顶层可见条目是否偏离 whitelist；
2. home 顶层可见条目是否偏离 homeWhitelist（兜住宿主应用静默建目录）；
3. Downloads / Desktop 等暂存目录是否有超期滞留文件；
4. 关键 launchd 任务是否在位、已加载、在运行（用户级 LaunchAgents 与第三方 LaunchDaemons）；
5. 正式项目（apps/）：git 仓库逐个查未推送提交、分支未设 upstream（不看未提交改动）；
6. 赛马项目（racing/）：按最近提交/修改时间算静默天数，分可清理候选与可跟进两档；
7. 本地补丁（patches/）：自用、不交付、不建仓库；按最近活动时间提示长期未动的复核候选；
8. lab（lab/）：实验目录按最近活动时间提示 60 天未动的可清理候选，自带 .git 的另查未提交改动；
9. upstream（upstream/）：第三方仓库只报 90 天无活动，不查未提交改动、不查未推送/落后远端；
10. 归档（archive/）：只报出现新提交（HEAD 变化；首次见到只播种不报），不看未提交改动；
11. 未登记分类：~/rain/projs 顶层目录超出登记集合时提示。
任一异常走统一推送通道；与上次异常集合相同则不重复打扰。每周五 17:30 那次运行在末尾追加本周汇总，数据按天累积在 week-state.json，不改 state.json 结构。手动调试见 README。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("RAIN_CONFIG", str(HERE / "config.json")))
STATE_PATH = Path(os.environ.get("RAIN_STATE_PATH", str(HERE / "state.json")))
LOG_PATH = HERE / "guard.log"
WEEK_STATE_PATH = Path(os.environ.get("RAIN_WEEK_STATE", str(HERE / "week-state.json")))
WEEKLY_WEEKDAY = 4  # 周五（date.weekday）
WEEKLY_RETAIN_DAYS = 14

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


DEFAULT_NOTIFY = os.path.expanduser("~/rain/projs/services/gw-push-gateway/notify")


def push(config: dict, title: str, content: str, kind: str = "todo") -> tuple[bool, str]:
    """统一推送入口：~/rain/projs/services/gw-push-gateway/notify。

    巡检发现异常属于「要你处理」，用 todo 类：先到通知栏，1 分钟没点开补微信。
    周汇总只告知、不需处理，调用时传 kind="info"，只到通知栏不补微信。
    """
    notify = os.path.expanduser(str(config.get("notifyScript") or DEFAULT_NOTIFY))
    try:
        result = subprocess.run(
            [notify, "--title", title, "--body", content, "--group", "guard", "--kind", kind],
            capture_output=True, text=True, timeout=40,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "notify 调用失败：%s" % exc
    detail = ((result.stdout or "") + (result.stderr or "")).strip()[:300]
    return result.returncode == 0, detail


def save_state(issues: list[str], archive_heads: dict | None = None) -> None:
    payload = {
        "issues": issues,
        "updated": datetime.now().isoformat(timespec="seconds"),
        "archiveHeads": dict(archive_heads or {}),
    }
    STATE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


# ---------- 每周汇总 ----------

def week_start(day: date) -> date:
    """本周周一。"""
    return day - timedelta(days=day.weekday())


def load_week_state() -> dict:
    raw = load_json(WEEK_STATE_PATH, {})
    days = raw.get("days") if isinstance(raw, dict) else None
    if not isinstance(days, dict):
        days = {}
    last = raw.get("lastSummaryWeek") if isinstance(raw, dict) else None
    return {"version": 1, "days": dict(days), "lastSummaryWeek": last or None}


def save_week_state(state: dict) -> None:
    WEEK_STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


def record_week_run(state: dict, issues: list[str], now: datetime) -> dict:
    """记录一次巡检结果；只留最近 WEEKLY_RETAIN_DAYS 天。"""
    state["days"][now.date().isoformat()] = {
        "issues": list(issues),
        "at": now.isoformat(timespec="seconds"),
    }
    cutoff = (now.date() - timedelta(days=WEEKLY_RETAIN_DAYS)).isoformat()
    state["days"] = {k: v for k, v in state["days"].items() if k >= cutoff}
    return state


def weekly_due(state: dict, today: date) -> bool:
    """周五且本周还没发过汇总。"""
    return (
        today.weekday() == WEEKLY_WEEKDAY
        and state.get("lastSummaryWeek") != week_start(today).isoformat()
    )


def _day_issue_text(issues: list[str], limit: int = 3) -> str:
    text = "；".join(issues[:limit])
    if len(issues) > limit:
        text += f"；等 {len(issues)} 项"
    return text[:220]


def weekly_summary(state: dict, today: date, current: list[str]) -> tuple[str, str]:
    """本周（周一到今天）汇总的标题与正文；全绿也返回内容，不静默。"""
    start = week_start(today)
    span = f"{start.strftime('%m-%d')} ~ {today.strftime('%m-%d')}"
    current_text = "全部正常" if not current else f"{len(current)} 项异常"
    recorded: list[tuple[str, list[str]]] = []
    day = start
    while day <= today:
        item = state.get("days", {}).get(day.isoformat())
        if isinstance(item, dict):
            recorded.append((day.strftime("%m-%d"), list(item.get("issues") or [])))
        day += timedelta(days=1)
    total = len(recorded)
    green = sum(1 for _, issues in recorded if not issues)
    bad = total - green
    missing = (today.weekday() + 1) - total
    missing_note = f"，另有 {missing} 天没有记录" if missing > 0 else ""
    if total == 0:
        return (
            "本周巡检汇总：暂无记录",
            f"本周（{span}）暂无巡检记录。\n当前状态：{current_text}。",
        )
    if bad == 0:
        return (
            f"本周巡检汇总：{total} 次全部正常",
            f"本周 {total} 次巡检全部正常（{span}{missing_note}）。\n当前状态：{current_text}。",
        )
    lines = [f"本周（{span}）共巡检 {total} 次：{green} 次全部正常，{bad} 次有异常。", "异常记录："]
    for label, issues in recorded:
        if issues:
            lines.append(f"· {label}（{len(issues)} 项）：{_day_issue_text(issues)}")
    if missing > 0:
        lines.append(f"另有 {missing} 天没有记录（未跑或数据缺失）。")
    lines.append(f"当前状态：{current_text}。")
    body = "\n".join(lines)[:1200]
    return f"本周巡检汇总：{green} 正常 / {bad} 有异常", body


def run_weekly(config: dict, issues: list[str], now: datetime | None = None) -> int:
    """记录本次结果；周五且本周未发过时推一条汇总（info 类）。"""
    now = now or datetime.now()
    try:
        state = record_week_run(load_week_state(), issues, now)
        save_week_state(state)
    except OSError as exc:
        log(f"周记失败：{exc}")
        return 0
    today = now.date()
    if not weekly_due(state, today):
        return 0
    title, body = weekly_summary(state, today, issues)
    ok, detail = push(config, title, body, kind="info")
    log(f"周报{'推送成功' if ok else '推送失败'}：{title} | {detail[:200]}")
    if ok:
        state["lastSummaryWeek"] = week_start(today).isoformat()
        save_week_state(state)
    return 0 if ok else 1


def weekly_preview(issues: list[str], now: datetime | None = None) -> tuple[str, str]:
    """只读预览：把本次结果并入内存副本后生成汇总，不落盘、不推送。"""
    now = now or datetime.now()
    state = record_week_run(load_week_state(), issues, now)
    return weekly_summary(state, now.date(), issues)


def preview_now() -> datetime:
    """预览/测试用：--preview-date YYYY-MM-DD 替换日期，默认当前时间。"""
    try:
        if "--preview-date" in sys.argv:
            i = sys.argv.index("--preview-date")
            return datetime.combine(
                date.fromisoformat(sys.argv[i + 1]),
                datetime.now().time().replace(microsecond=0),
            )
    except (IndexError, ValueError):
        pass
    return datetime.now()


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


# ---------- 检查五 / 六：项目成熟度巡检 ----------

GIT_SENTINEL = object()
IGNORE_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".runtime", "dist", "vendor"}


def git_info(cwd: Path, *args: str):
    """在 cwd 跑 git 子命令；非仓库或失败返回 None。"""
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def is_git_repo(path: Path) -> bool:
    return git_info(path, "rev-parse", "--is-inside-work-tree") == "true"


def latest_activity(path: Path) -> tuple[float, str]:
    """返回 (时间戳, 来源)：git 末次提交与目录内文件 mtime 取较新者。"""
    stamps: list[tuple[float, str]] = []
    if is_git_repo(path):
        commit_ts = git_info(path, "log", "-1", "--format=%ct")
        if commit_ts and commit_ts.isdigit():
            stamps.append((float(commit_ts), "提交"))
    newest, source = 0.0, "文件"
    try:
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.startswith(".")]
            for name in files:
                if name.startswith("."):
                    continue
                try:
                    mtime = (Path(root) / name).stat().st_mtime
                except OSError:
                    continue
                if mtime > newest:
                    newest, source = mtime, "文件"
    except OSError:
        pass
    stamps.append((newest, source))
    return max(stamps, key=lambda item: item[0])


def check_formal_projects(block: dict) -> list[str]:
    """apps/ 下的正式项目：只查推送状态（未推送提交、分支未设 upstream）；工作区脏不脏不看。"""
    apps_dir = Path(os.path.expanduser(
        os.environ.get("RAIN_APPS_DIR") or block.get("dir") or str(ROOT / "projs" / "apps")
    ))
    if not apps_dir.is_dir():
        return [f"正式项目目录不存在：{apps_dir}"]

    # 豁免名单：已退场但按要求保留源码的本地项目，不再报项目卫生问题
    ignore = set(block.get("ignore") or [])
    issues: list[str] = []
    try:
        projects = sorted(p for p in apps_dir.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError as exc:
        return [f"扫描正式项目目录失败：{exc}"]

    for project in projects:
        if project.name in ignore:
            continue
        if not is_git_repo(project):
            issues.append(f"{project.name} 非 git 仓库：apps/ 要求 git + 远端 + 已推送，补齐或挪去 patches/")
            continue
        if not git_info(project, "remote"):
            issues.append(f"{project.name} 没有远端仓库（apps/ 要求建远端并推送）")
            continue
        # 用户 2026-11-26 定：工作区有未提交改动不算问题，只有「有提交没推送」才算
        unpushed = git_info(project, "rev-list", "--count", "@{upstream}..HEAD")
        if unpushed and unpushed != "0":
            issues.append(f"{project.name} 有 {unpushed} 个提交未推送")
        elif git_info(project, "rev-parse", "@{upstream}") is None:
            branch = git_info(project, "rev-parse", "--abbrev-ref", "HEAD") or "HEAD"
            issues.append(f"{project.name} 分支 {branch} 未设 upstream（无法确认推送）")
    return issues


def check_racing_projects(block: dict) -> list[str]:
    """racing/ 下的赛马项目：按静默天数分可清理候选与可跟进两档。"""
    racing_dir = Path(os.path.expanduser(
        os.environ.get("RAIN_RACING_DIR") or block.get("dir") or str(ROOT / "projs" / "racing")
    ))
    if not racing_dir.is_dir():
        return [f"赛马项目目录不存在：{racing_dir}"]

    stale_days = float(block.get("staleDays", 30))
    active_days = float(block.get("activeDays", 7))
    now = datetime.now().timestamp()
    stale: list[str] = []
    active: list[str] = []
    try:
        projects = sorted(p for p in racing_dir.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError as exc:
        return [f"扫描赛马项目目录失败：{exc}"]

    for project in projects:
        ts, source = latest_activity(project)
        if ts == 0.0:
            continue
        idle_days = (now - ts) / 86400
        label = f"{project.name}（{int(idle_days)} 天未动，按{source}）"
        if idle_days >= stale_days:
            stale.append(label)
        elif idle_days <= active_days:
            active.append(label)
    issues: list[str] = []
    if stale:
        issues.append(f"赛马可清理候选（≥{int(stale_days)}天）：" + "、".join(stale))
    if active:
        issues.append(f"赛马近期活跃可跟进（≤{int(active_days)}天）：" + "、".join(active))
    return issues


def check_patch_projects(block: dict) -> list[str]:
    """patches/ 下的本地补丁：自用、不交付、不建仓库；只提示长期未动的复核候选。

    只看长期未动的复核候选；工作区脏不脏、有没有远端都不看（用户 2026-11-26 定）。
    """
    patch_dir = Path(os.path.expanduser(
        os.environ.get("RAIN_PATCH_DIR") or block.get("dir") or str(ROOT / "projs" / "patches")
    ))
    if not patch_dir.is_dir():
        return [f"本地补丁目录不存在：{patch_dir}"]

    stale_days = float(block.get("staleDays", 60))
    now = datetime.now().timestamp()
    issues: list[str] = []
    stale: list[str] = []
    try:
        projects = sorted(p for p in patch_dir.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError as exc:
        return [f"扫描本地补丁目录失败：{exc}"]

    for project in projects:
        ts, source = latest_activity(project)
        if ts == 0.0:
            continue
        idle_days = (now - ts) / 86400
        if idle_days >= stale_days:
            stale.append(f"{project.name}（{int(idle_days)} 天未动，按{source}）")
    if stale:
        issues.append(f"本地补丁长期未动、可复核是否仍需要（≥{int(stale_days)}天）：" + "、".join(stale))
    return issues


# ---------- 检查八：lab（实验目录） ----------

def check_lab_projects(block: dict) -> list[str]:
    """lab/ 下的实验目录：自用、不交付，只提示长期未动的可清理候选（与 patches 同口径）。

    只看长期未动的复核候选；工作区脏不脏、有没有远端都不看（用户 2026-11-26 定）。
    """
    lab_dir = Path(os.path.expanduser(
        os.environ.get("RAIN_LAB_DIR") or block.get("dir") or str(ROOT / "projs" / "lab")
    ))
    if not lab_dir.is_dir():
        return [f"lab 目录不存在：{lab_dir}"]

    stale_days = float(block.get("staleDays", 60))
    now = datetime.now().timestamp()
    issues: list[str] = []
    stale: list[str] = []
    try:
        projects = sorted(p for p in lab_dir.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError as exc:
        return [f"扫描 lab 目录失败：{exc}"]

    for project in projects:
        ts, source = latest_activity(project)
        if ts == 0.0:
            continue
        idle_days = (now - ts) / 86400
        if idle_days >= stale_days:
            stale.append(f"{project.name}（{int(idle_days)} 天未动，按{source}）")
    if stale:
        issues.append(f"lab 可清理候选（≥{int(stale_days)}天）：" + "、".join(stale))
    return issues


# ---------- 检查九：upstream（第三方仓库） ----------

def check_upstream_projects(block: dict) -> list[str]:
    """upstream/ 下的第三方仓库：只报超期无活动，不查未提交改动、不查未推送/落后远端。"""
    upstream_dir = Path(os.path.expanduser(
        os.environ.get("RAIN_UPSTREAM_DIR") or block.get("dir") or str(ROOT / "projs" / "upstream")
    ))
    if not upstream_dir.is_dir():
        return [f"upstream 目录不存在：{upstream_dir}"]

    stale_days = float(block.get("staleDays", 90))
    now = datetime.now().timestamp()
    issues: list[str] = []
    try:
        projects = sorted(p for p in upstream_dir.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError as exc:
        return [f"扫描 upstream 目录失败：{exc}"]

    for project in projects:
        ts, source = latest_activity(project)
        if ts == 0.0:
            continue
        idle_days = (now - ts) / 86400
        if idle_days >= stale_days:
            issues.append(f"{project.name} 已 {int(idle_days)} 天无活动（阈值 {int(stale_days)} 天）")
    return issues


# ---------- 检查十：archive（归档） ----------

def check_archive_projects(block: dict,
                           known_heads: dict | None = None) -> tuple[list[str], dict]:
    """archive/ 下的归档目录：只报出现新提交（不看工作区脏不脏）。

    HEAD 记在 state 的 archiveHeads 里：首次见到只播种、不报；之后 HEAD 变化即报「归档项目出现新提交」。
    目录为空、或只有 README.md 时不报；非 git 目录不参与新提交判断。
    """
    archive_dir = Path(os.path.expanduser(
        os.environ.get("RAIN_ARCHIVE_DIR") or block.get("dir") or str(ROOT / "projs" / "archive")
    ))
    known = dict(known_heads or {})
    if not archive_dir.is_dir():
        return ([f"归档目录不存在：{archive_dir}"], known)

    issues: list[str] = []
    heads: dict[str, str] = {}
    try:
        projects = sorted(p for p in archive_dir.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError as exc:
        return ([f"扫描归档目录失败：{exc}"], known)

    for project in projects:
        try:
            entries = [e for e in project.iterdir() if not e.name.startswith(".")]
        except OSError:
            continue
        if not entries:
            continue
        if len(entries) == 1 and entries[0].is_file() and entries[0].name == "README.md":
            continue
        if not is_git_repo(project):
            continue
        head = git_info(project, "rev-parse", "HEAD")
        if head:
            prev = known.get(project.name)
            if prev and prev != head:
                issues.append(f"{project.name} 归档项目出现新提交（{prev[:8]}→{head[:8]}）")
            heads[project.name] = head
    return issues, heads


# ---------- 检查十一：projs 顶层未登记分类 ----------

REGISTRY = {"apps", "racing", "patches", "services", "upstream", "lab", "archive", "agent-rules"}


def check_unregistered_projects(block: dict) -> list[str]:
    """~/rain/projs 顶层：目录超出登记集合时报「未登记分类」；文件与隐藏项不看。"""
    projs = Path(os.path.expanduser(
        os.environ.get("RAIN_PROJS_DIR") or str(ROOT / "projs")
    ))
    if not projs.is_dir():
        return [f"projs 根目录不存在：{projs}"]

    registry = set(block.get("registry") or REGISTRY)
    try:
        entries = sorted(p.name for p in projs.iterdir()
                         if p.is_dir() and not p.name.startswith("."))
    except OSError as exc:
        return [f"扫描 projs 根目录失败：{exc}"]

    unexpected = [name for name in entries if name not in registry]
    if unexpected:
        return ["未登记分类：" + "、".join(unexpected)]
    return []


# ---------- 通知排版：只列异常、按紧急度排序，正常项折叠 ----------

# 服务掉线最急，其次是目录基线，项目成熟度类靠后。
SECTION_RANK = (
    ("LaunchAgent", 0), ("LaunchDaemon", 0), ("launchd", 0),
    ("~/rain", 1), ("home 顶层", 1), ("暂存目录", 2),
    ("正式项目", 3), ("赛马项目", 4), ("本地补丁", 5),
    ("lab", 6), ("upstream", 7), ("归档", 8), ("未登记", 9),
)


def section_rank(name: str) -> int:
    for prefix, rank in SECTION_RANK:
        if prefix in name:
            return rank
    return 9


def render_issue(line: str) -> list[str]:
    """把「前缀：a、b、c」这类长串拆成小标题 + 逐项；短句保持一行。"""
    head, sep, tail = line.partition("：")
    if sep and "、" in tail and len(line) > 36:
        parts = [p.strip() for p in re.split(r"[、；]", tail) if p.strip()]
        rows = [f"{head.rstrip()} — {len(parts)} 项"]
        rows.extend(f"  · {p}" for p in parts[:12])
        if len(parts) > 12:
            rows.append(f"  · 另有 {len(parts) - 12} 项未列出")
        return rows
    return [f"· {line}"]


def render_body(sections: list[tuple[str, list[str]]]) -> str:
    """异常分块按紧急度列出，正常分块只留一行计数。"""
    abnormal = [(name, block) for name, block in sections if block]
    abnormal.sort(key=lambda item: section_rank(item[0]))
    lines: list[str] = []
    for name, block in abnormal:
        lines.append(name)
        for issue in block:
            lines.extend(render_issue(issue))
    normal = len(sections) - len(abnormal)
    if normal:
        lines.append(f"其余 {normal} 块正常")
    # PushPlus 走 markdown 模板：单个 \n 在微信里会被并成一段，行尾两个空格才是硬换行；
    # Bark 只当行尾空白处理，不影响显示。
    return "  \n".join(lines) + "  "


def main() -> int:
    config = load_json(CONFIG_PATH, {})
    if "--test-push" in sys.argv:
        ok, detail = push(config, "rain-guard 通道测试", "收到这条说明巡检推送通道正常。")
        print(ok, detail[:200])
        return 0 if ok else 1

    dry = "--dry-run" in sys.argv
    prev = load_json(STATE_PATH, {})
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
            sections.append((kind, check_launchd(kind, directory, block.get("expected") or {}, svc)))

    formal = config.get("formalProjects") or {}
    if formal.get("enabled", True):
        sections.append(("正式项目 apps/", check_formal_projects(formal)))

    racing = config.get("racingProjects") or {}
    if racing.get("enabled", True):
        sections.append(("赛马项目 racing/", check_racing_projects(racing)))

    patch = config.get("patchProjects") or {}
    if patch.get("enabled", True):
        sections.append(("本地补丁 patches/", check_patch_projects(patch)))

    lab = config.get("labProjects") or {}
    if lab.get("enabled", True):
        sections.append(("lab 项目 lab/", check_lab_projects(lab)))

    upstream = config.get("upstreamProjects") or {}
    if upstream.get("enabled", True):
        sections.append(("upstream 第三方仓库", check_upstream_projects(upstream)))

    archive = config.get("archiveProjects") or {}
    archive_heads: dict[str, str] = {}
    if archive.get("enabled", True):
        archive_issues, archive_heads = check_archive_projects(archive, prev.get("archiveHeads") or {})
        sections.append(("归档项目 archive/", archive_issues))

    unregistered = config.get("unregisteredProjects") or {}
    if unregistered.get("enabled", True):
        sections.append(("未登记分类 projs/", check_unregistered_projects(unregistered)))

    issues = [line for _, block in sections for line in block]
    title = f"下班前巡检：{len(issues)} 项异常" if issues else "下班前巡检：全部正常"
    body = render_body(sections) if issues else ""

    if "--weekly-preview" in sys.argv:
        now = preview_now()
        wtitle, wbody = weekly_preview(issues, now)
        print(f"[weekly-preview] {wtitle}\n{wbody}")
        return 0

    if dry:
        print(f"[dry-run] {title}\n{body}")
        now = preview_now()
        if now.date().weekday() == WEEKLY_WEEKDAY:
            wtitle, wbody = weekly_preview(issues, now)
            print(f"[dry-run 周报] {wtitle}\n{wbody}")
        return 0

    if "issues" not in prev:  # V1.0 旧状态只有 root 差异，按空集比较并升级格式
        prev["issues"] = []
    if sorted(issues) == sorted(prev["issues"]):
        if "updated" not in prev or archive_heads != (prev.get("archiveHeads") or {}):
            save_state(issues, archive_heads)
        log("无异常" if not issues else "异常未变化，跳过重复推送")
        return run_weekly(config, issues)

    if issues:
        ok, detail = push(config, title, body)
        log(f"推送{'成功' if ok else '失败'}：{title} | {detail[:200]}")
        rc = 0 if ok else 1
    else:
        log("已恢复正常，未推送")
        rc = 0
    save_state(issues, archive_heads)
    weekly_rc = run_weekly(config, issues)
    return 0 if rc == 0 and weekly_rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
