"""Incremental scanners for every place Claude writes session activity on this Mac.

* Claude Code CLI / VS Code / Desktop "Code" tab: ~/.claude/projects/**/*.jsonl
  (the desktop app runs Claude Code under the hood; `entrypoint` tells them apart)
* Cowork: ~/Library/Application Support/Claude/local-agent-mode-sessions/*/*/local_*/audit.jsonl
"""

import json
import re
import sqlite3
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from . import store

HOME = Path.home()
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
DESKTOP_DIR = HOME / "Library" / "Application Support" / "Claude"
COWORK_DIR = DESKTOP_DIR / "local-agent-mode-sessions"
DESKTOP_CODE_DIR = DESKTOP_DIR / "claude-code-sessions"

ACTIVITY_TYPES = {"user", "assistant"}
ENTRYPOINT_SOURCES = {
    "cli": "CLI",
    "claude-desktop": "Desktop",
    "claude-vscode": "VS Code",
}
WORKTREE_RE = re.compile(r"/\.claude/worktrees/[^/]+.*$")


def parse_ts(value: str) -> int | None:
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except (ValueError, AttributeError):
        return None


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def worktree_map() -> dict[str, str]:
    """Map worktree checkouts back to the repo they belong to."""
    mapping: dict[str, str] = {}
    for wt in load_json(DESKTOP_DIR / "git-worktrees.json").get("worktrees", {}).values():
        if wt.get("path") and wt.get("baseRepo"):
            mapping[wt["path"]] = wt["baseRepo"]
    for meta in DESKTOP_CODE_DIR.glob("*/*/local_*.json"):
        data = load_json(meta)
        cwd, origin = data.get("cwd"), data.get("originCwd")
        if cwd and origin and cwd != origin:
            mapping[cwd] = origin
    return mapping


def normalize_workspace(cwd: str, worktrees: dict[str, str]) -> str:
    for wt_path, base in worktrees.items():
        if cwd == wt_path or cwd.startswith(wt_path + "/"):
            return base
    return WORKTREE_RE.sub("", cwd).rstrip("/") or cwd


def read_new_lines(conn: sqlite3.Connection, path: Path) -> Iterator[dict]:
    """Yield JSON records appended since the last scan, then persist the new offset."""
    try:
        size = path.stat().st_size
    except OSError:
        return
    known_size, offset = store.file_offset(conn, str(path))
    if size == known_size:
        return
    if size < offset:  # file was rewritten/truncated: start over
        offset = 0
    with path.open("rb") as fh:
        fh.seek(offset)
        chunk = fh.read()
    end = chunk.rfind(b"\n")
    if end < 0:
        return
    for line in chunk[: end + 1].splitlines():
        try:
            yield json.loads(line)
        except ValueError:
            continue
    store.set_file_offset(conn, str(path), size, offset + end + 1)


def scan_claude_code(conn: sqlite3.Connection, worktrees: dict[str, str]) -> int:
    added = 0
    # main transcripts before their subagents/, so subagents inherit the right source
    for path in sorted(CLAUDE_PROJECTS.rglob("*.jsonl"), key=lambda p: len(p.parts)):
        if "local-agent-mode-sessions" in path.parts[len(CLAUDE_PROJECTS.parts)]:
            continue  # Cowork transcripts are picked up from audit.jsonl instead
        rows: list[tuple[str, int, str, str]] = []
        titles: dict[tuple[str, bool], str] = {}
        for rec in read_new_lines(conn, path):
            session = rec.get("sessionId")
            if rec.get("type") == "ai-title" and session and rec.get("aiTitle"):
                titles[(session, False)] = rec["aiTitle"]
            if rec.get("type") == "custom-title" and session and rec.get("customTitle"):
                titles[(session, True)] = rec["customTitle"]
            if rec.get("type") not in ACTIVITY_TYPES or not session or not rec.get("cwd"):
                continue
            ts = parse_ts(rec.get("timestamp", ""))
            if ts is None:
                continue
            known = store.session_info(conn, session)
            entry = rec.get("entrypoint")
            if known:
                # Claude `cd`s into subfolders mid-session; the workspace stays
                # the folder the session was started in.
                workspace, source = known
            else:
                workspace = normalize_workspace(rec["cwd"], worktrees)
                # subagent transcripts carry no entrypoint
                source = ENTRYPOINT_SOURCES.get(entry, entry) if entry else "CLI"
                store.upsert_session(conn, session, workspace, source, None)
            rows.append((session, ts, workspace, source))
        store.add_events(conn, rows)
        for (session, custom), title in titles.items():
            store.set_title(conn, session, title, custom)
        added += len(rows)
    return added


def scan_desktop_titles(conn: sqlite3.Connection) -> None:
    """The Desktop app keeps the title shown in its sidebar in its own metadata.

    Its "auto" titles are just the start of the first message, so those lose to
    the transcript's ai-title; anything else (renamed, older sessions) wins.
    """
    for meta in DESKTOP_CODE_DIR.glob("*/*/local_*.json"):
        data = load_json(meta)
        if data.get("cliSessionId") and data.get("title") and data.get("titleSource") != "auto":
            store.set_title(conn, data["cliSessionId"], data["title"], custom=True)


def cowork_workspace(meta: dict, spaces: dict[str, str]) -> str:
    if meta.get("spaceId") in spaces:
        return f"cowork:{spaces[meta['spaceId']]}"
    folders = meta.get("userSelectedFolders") or []
    if folders:
        return folders[0].rstrip("/")
    return "cowork:(no folder)"


def scan_cowork(conn: sqlite3.Connection) -> int:
    added = 0
    for account_dir in COWORK_DIR.glob("*/*"):
        spaces = {
            s["id"]: s.get("name", s["id"])
            for s in load_json(account_dir / "spaces.json").get("spaces", [])
            if "id" in s
        }
        for audit in account_dir.glob("local_*/audit.jsonl"):
            session_dir = audit.parent
            meta = load_json(session_dir.with_suffix(".json"))
            session = meta.get("sessionId") or session_dir.name
            workspace = cowork_workspace(meta, spaces)
            store.upsert_session(conn, session, workspace, "Cowork", meta.get("title"))
            rows = []
            for rec in read_new_lines(conn, audit):
                if rec.get("type") not in ACTIVITY_TYPES:
                    continue
                ts = parse_ts(rec.get("_audit_timestamp", ""))
                if ts is not None:
                    rows.append((session, ts, workspace, "Cowork"))
            store.add_events(conn, rows)
            added += len(rows)
    return added


def scan(conn: sqlite3.Connection) -> int:
    worktrees = worktree_map()
    added = scan_claude_code(conn, worktrees) + scan_cowork(conn)
    scan_desktop_titles(conn)
    conn.commit()
    return added
