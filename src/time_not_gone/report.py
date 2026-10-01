"""Turn raw activity timestamps into "active time" per workspace.

Two consecutive events in the same workspace less than IDLE_GAP apart count as
continuous work. Every burst of activity also gets a small TAIL credit after
its last event (reading the answer, thinking). Parallel sessions in one
workspace are merged, so they are never double counted; the overall total is
the union across all workspaces.
"""

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

from . import store

IDLE_GAP = 30 * 60
TAIL = 60


@dataclass
class SessionTime:
    session: str
    source: str
    seconds: int = 0
    first: int | None = None
    last: int | None = None
    title: str | None = None

    def merge(self, other: "SessionTime") -> None:
        """Add another day of the same session (first/last are always set in reports)."""
        self.seconds += other.seconds
        self.first = min(self.first or other.first, other.first or self.first)
        self.last = max(self.last or other.last, other.last or self.last)

    def to_dict(self) -> dict:
        return {
            "id": self.session,
            "title": self.title or "Untitled session",
            "source": self.source,
            "seconds": self.seconds,
            "first": self.first,
            "last": self.last,
        }


@dataclass
class WorkspaceTime:
    key: str
    seconds: int = 0
    sources: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    sessions: set[str] = field(default_factory=set)
    first: int | None = None
    last: int | None = None
    # time per session, from that session's own messages; overlapping sessions
    # can add up to more than `seconds`
    session_times: dict[str, SessionTime] = field(default_factory=dict)

    def session_list(self) -> list[SessionTime]:
        return sorted(self.session_times.values(), key=lambda s: s.first or 0, reverse=True)

    @property
    def name(self) -> str:
        if self.key.startswith("cowork:"):
            return f"Cowork · {self.key.removeprefix('cowork:')}"
        return Path(self.key).name or self.key

    @property
    def path(self) -> str:
        if self.key.startswith("cowork:"):
            return ""
        home = str(Path.home())
        return "~" + self.key[len(home):] if self.key.startswith(home) else self.key

    def source_split(self) -> dict[str, int]:
        """Per-source seconds scaled so they add up to the (de-duplicated) total."""
        raw = sum(self.sources.values()) or 1
        return {
            src: round(secs * self.seconds / raw)
            for src, secs in sorted(self.sources.items(), key=lambda kv: -kv[1])
        }

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "path": self.path,
            "seconds": self.seconds,
            "sources": self.source_split(),
            "sessions": len(self.sessions),
            "first": self.first,
            "last": self.last,
            "session_list": [s.to_dict() for s in self.session_list()],
        }


@dataclass
class DayReport:
    day: date
    total: int
    workspaces: list[WorkspaceTime]

    def to_dict(self) -> dict:
        return {
            "date": self.day.isoformat(),
            "total_seconds": self.total,
            "workspaces": [w.to_dict() for w in self.workspaces],
        }


def day_bounds(day: date) -> tuple[int, int]:
    start = datetime.combine(day, time.min).astimezone()
    end = datetime.combine(day + timedelta(days=1), time.min).astimezone()
    return int(start.timestamp()), int(end.timestamp())


def merged_length(segments: list[tuple[int, int]]) -> int:
    total, cur_start, cur_end = 0, None, None
    for a, b in sorted(segments):
        if cur_end is None or a > cur_end:
            if cur_end is not None:
                total += cur_end - cur_start
            cur_start, cur_end = a, b
        else:
            cur_end = max(cur_end, b)
    if cur_end is not None:
        total += cur_end - cur_start
    return total


def active_segments(
    times: list[int], start: int, end: int, idle_gap: int
) -> list[tuple[int, int, int]]:
    """(from, to, index of the event that opened it) of active time, clipped to [start, end)."""
    segments = []
    for i, ts in enumerate(times):
        nxt = times[i + 1] if i + 1 < len(times) else None
        seg_end = nxt if nxt is not None and nxt - ts <= idle_gap else ts + TAIL
        a, b = max(ts, start), min(seg_end, end)
        if b > a:
            segments.append((a, b, i))
    return segments


def session_times(
    events: list[tuple[int, str, str]], start: int, end: int, idle_gap: int
) -> dict[str, SessionTime]:
    by_session: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for ts, source, session in events:
        by_session[session].append((ts, source))
    result = {}
    for session, evs in by_session.items():
        times = [ts for ts, _ in evs]
        st = SessionTime(session, evs[-1][1])
        st.seconds = merged_length([(a, b) for a, b, _ in active_segments(times, start, end, idle_gap)])
        in_day = [ts for ts in times if start <= ts < end]
        if st.seconds > 0 and in_day:
            st.first, st.last = in_day[0], in_day[-1]
            result[session] = st
    return result


def day_report(conn: sqlite3.Connection, day: date, idle_gap: int = IDLE_GAP) -> DayReport:
    start, end = day_bounds(day)
    by_ws: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
    for session, ts, workspace, source in store.events_between(conn, start - idle_gap, end + idle_gap):
        by_ws[workspace].append((ts, source, session))

    all_segments: list[tuple[int, int]] = []
    results: list[WorkspaceTime] = []
    for key, events in by_ws.items():
        wt = WorkspaceTime(key)
        segments: list[tuple[int, int]] = []
        for a, b, i in active_segments([ts for ts, _, _ in events], start, end, idle_gap):
            segments.append((a, b))
            wt.sources[events[i][1]] += b - a
        for ts, source, session in events:
            if start <= ts < end:
                wt.sessions.add(session)
                wt.first = ts if wt.first is None else wt.first
                wt.last = ts
        wt.seconds = merged_length(segments)
        if wt.seconds > 0:
            wt.session_times = session_times(events, start, end, idle_gap)
            results.append(wt)
            all_segments.extend(segments)

    titles = store.session_titles(conn, [s for w in results for s in w.session_times])
    for w in results:
        for st in w.session_times.values():
            st.title = titles.get(st.session)
    results.sort(key=lambda w: -w.seconds)
    return DayReport(day, merged_length(all_segments), results)


def range_reports(
    conn: sqlite3.Connection, last_day: date, days: int, idle_gap: int = IDLE_GAP
) -> list[DayReport]:
    return [day_report(conn, last_day - timedelta(days=i), idle_gap) for i in range(days - 1, -1, -1)]


def combine(reports: list[DayReport]) -> list[WorkspaceTime]:
    combined: dict[str, WorkspaceTime] = {}
    for rep in reports:
        for w in rep.workspaces:
            agg = combined.setdefault(w.key, WorkspaceTime(w.key))
            agg.seconds += w.seconds
            agg.sessions |= w.sessions
            for src, secs in w.sources.items():
                agg.sources[src] += secs
            agg.first = w.first if agg.first is None else min(agg.first, w.first or agg.first)
            agg.last = w.last if agg.last is None else max(agg.last, w.last or agg.last)
            for sid, st in w.session_times.items():
                if sid in agg.session_times:
                    agg.session_times[sid].merge(st)
                else:
                    agg.session_times[sid] = replace(st)
    return sorted(combined.values(), key=lambda w: -w.seconds)
