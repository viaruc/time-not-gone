"""`tng` — how much time did Claude and I spend in each workspace?"""

import argparse
import json
import sys
import time as systime
from datetime import date, datetime, timedelta

from . import report, sources, store
from .report import DayReport, WorkspaceTime

BAR_WIDTH = 24
BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"


def fmt_duration(seconds: int) -> str:
    minutes = round(seconds / 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def parse_day(value: str) -> date:
    today = date.today()
    if value in ("today", ""):
        return today
    if value == "yesterday":
        return today - timedelta(days=1)
    if value.lstrip("-").isdigit():  # -1 = yesterday, -2 = day before, ...
        return today + timedelta(days=int(value))
    return date.fromisoformat(value)


def style(code: str, text: str, color: bool) -> str:
    return f"{code}{text}{RESET}" if color else text


def print_table(title: str, total: int, workspaces: list[WorkspaceTime], color: bool) -> None:
    print(style(BOLD, f"{title}  —  {fmt_duration(total)} total", color))
    if not workspaces:
        print(style(DIM, "  no activity", color))
        return
    top = workspaces[0].seconds or 1
    name_w = min(max(len(w.name) for w in workspaces), 32)
    for w in workspaces:
        bar = "█" * max(1, round(BAR_WIDTH * w.seconds / top))
        split = ", ".join(f"{src} {fmt_duration(s)}" for src, s in w.source_split().items() if s >= 60)
        print(
            f"  {w.name[:name_w]:<{name_w}}  {fmt_duration(w.seconds):>8}  {bar:<{BAR_WIDTH}}  "
            + style(DIM, f"{len(w.sessions)} sess · {split}", color)
        )


def print_week_strip(reports: list[DayReport], color: bool) -> None:
    top = max((r.total for r in reports), default=0) or 1
    for r in reports:
        bar = "▇" * round(BAR_WIDTH * r.total / top)
        print(f"  {r.day:%a %d %b}  {fmt_duration(r.total):>8}  {bar}")
    print()


def cmd_day(conn, args) -> None:
    rep = report.day_report(conn, parse_day(args.day), args.idle * 60)
    if args.json:
        print(json.dumps(rep.to_dict(), indent=2))
        return
    print_table(f"{rep.day:%A, %d %B %Y}", rep.total, rep.workspaces, args.color)


def cmd_range(conn, args, days: int) -> None:
    reps = report.range_reports(conn, parse_day(args.day), days, args.idle * 60)
    combined = report.combine(reps)
    total = sum(r.total for r in reps)
    if args.json:
        print(json.dumps({
            "days": [r.to_dict() for r in reps],
            "total_seconds": total,
            "workspaces": [w.to_dict() for w in combined],
        }, indent=2))
        return
    print_table(f"Last {days} days (to {reps[-1].day:%d %b})", total, combined, args.color)
    print()
    print_week_strip(reps, args.color)


def cmd_menubar(conn, args) -> None:
    """Everything the menubar app needs, in one call."""
    day = parse_day(args.day)
    reps = report.range_reports(conn, day, 7, args.idle * 60)
    print(json.dumps({
        "generated_at": int(systime.time()),
        "today": reps[-1].to_dict(),
        "week": {
            "days": [{
                "date": r.day.isoformat(),
                "total_seconds": r.total,
                "workspaces": [{"key": w.key, "name": w.name, "seconds": w.seconds} for w in r.workspaces],
            } for r in reps],
            "total_seconds": sum(r.total for r in reps),
            "workspaces": [w.to_dict() for w in report.combine(reps)],
        },
    }))


def cmd_watch(conn, args) -> None:
    while True:
        added = sources.scan(conn)
        if added:
            print(f"{datetime.now():%H:%M:%S}  +{added} events", flush=True)
        systime.sleep(args.interval)


def main() -> None:
    parser = argparse.ArgumentParser(prog="tng", description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--idle", type=int, default=report.IDLE_GAP // 60,
                        help=f"minutes without activity before a gap counts as idle (default {report.IDLE_GAP // 60})")
    parser.add_argument("--no-scan", action="store_true", help="skip indexing new activity first")
    sub = parser.add_subparsers(dest="cmd")
    for name, help_text in [
        ("today", "time per workspace today (default)"),
        ("day", "time per workspace on a given day"),
        ("week", "last 7 days"),
        ("month", "last 30 days"),
        ("menubar", "JSON payload for the menubar app"),
    ]:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("day", nargs="?", default="today",
                       help="YYYY-MM-DD, 'yesterday' or -N (default today)")
    sub.add_parser("scan", help="index new activity and exit")
    watch = sub.add_parser("watch", help="keep indexing in the foreground")
    watch.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()
    args.cmd = args.cmd or "today"
    args.day = getattr(args, "day", "today")
    args.color = sys.stdout.isatty()

    conn = store.connect()
    if args.cmd == "watch":
        cmd_watch(conn, args)
        return
    if not args.no_scan:
        added = sources.scan(conn)
        if args.cmd == "scan":
            print(f"indexed {added} new events → {store.DB_PATH}")
            return

    if args.cmd in ("today", "day"):
        cmd_day(conn, args)
    elif args.cmd == "week":
        cmd_range(conn, args, 7)
    elif args.cmd == "month":
        cmd_range(conn, args, 30)
    elif args.cmd == "menubar":
        cmd_menubar(conn, args)
