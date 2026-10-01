# time-not-gone

A macOS menubar app and CLI that shows how much time you spend with Claude on each
workspace each day.

If you run Claude in many places at once (the terminal, VS Code, the Desktop app's Code
tab, Cowork), it's hard to tell where your day went. time-not-gone reads the session logs
Claude already writes on your Mac and turns them into a daily breakdown per project folder.
You don't need to install any hooks or change how you work.

```
Thursday, 01 October 2026  —  1h 35m total
  izzywell                56m  ████████████████████████  1 sess · Desktop 56m
  time-not-gone           15m  ██████                    1 sess · Desktop 15m
  rewatch-tma-store       12m  █████                     1 sess · Desktop 12m
  idea0926                12m  █████                     2 sess · VS Code 12m
```

## What it tracks

| Where you use Claude | Where the activity is read from |
| --- | --- |
| Claude Code CLI | `~/.claude/projects/**/*.jsonl` |
| Claude Code in VS Code | same, told apart by the log's `entrypoint` field |
| Claude Desktop, Code tab | same (the Desktop app runs Claude Code under the hood) |
| Claude Desktop, Cowork | `~/Library/Application Support/Claude/local-agent-mode-sessions/*/*/local_*/audit.jsonl` |

**Workspace** means the folder a session was started in. Two details:

- If Claude `cd`s into a subfolder during a session, the time still goes to the folder
  the session started in.
- Desktop worktrees (`.claude/worktrees/…`, `~/.claude-worktrees/…`) count toward the
  repo they came from.

Cowork sessions are grouped by their Space name (shown as `Cowork · Name`). A Cowork
session with no Space goes under the first folder you shared with it.

## How time is counted

Time is estimated from the timestamps of your prompts and Claude's replies:

- **Continuous work:** two messages in the same workspace less than 30 minutes apart
  count as continuous time. Change the cutoff with `--idle <minutes>`.
- **Tail credit:** each burst of activity gets one extra minute at the end, for reading
  the last reply.
- **Parallel sessions:** several sessions open at once in the same workspace are counted
  once, not added together.
- **Daily total:** the real amount of time you had any Claude session going, so it's
  usually less than the sum of the per-workspace times.

So the number means **time working with Claude**, not total time on the project. If you
spend an hour reading a doc or in a meeting between two prompts, that hour isn't counted.

## History is kept

Claude Code deletes session logs after about 30 days. time-not-gone copies every activity
event into its own SQLite database, so your history outlives those logs:

```
~/Library/Application Support/time-not-gone/tng.db
```

Indexing is incremental: each scan only reads what was added to the logs since the last
one. The first full scan of ~40k events takes about 1.5s, and after that a report takes
well under a second. Keep the menubar app (or `tng watch`) running at least every few
weeks so nothing is deleted before it's copied.

## Install

You need macOS 14+, [uv](https://docs.astral.sh/uv/) and the Xcode command-line tools
(for `swiftc`).

```bash
uv tool install -e .
```

```bash
./menubar/build.sh --install
```

The first command puts `tng` in `~/.local/bin`. The second builds
`~/Applications/TimeNotGone.app` and installs it there. Open the app, then tick
**Open at login** in its popover to start it automatically.

## Menubar app

The menu bar shows an hourglass and today's total. Click it to open the popover:

- **Day** lists each workspace's time. Each bar is split by where you used Claude:
  Desktop, VS Code, CLI or Cowork.
- **7 days** shows a stacked bar chart of the last week, one colour per workspace. The
  list below it uses the same colours, so it doubles as the legend.
- **‹ ›** steps back and forward through days.

The app refreshes every minute by running `tng menubar`. It looks for `tng` in
`~/.local/bin`, `/opt/homebrew/bin` and `/usr/local/bin`. If yours is somewhere else, set
`TNG_PATH`.

## CLI

```bash
tng                  # today
tng day yesterday    # a given day: yesterday, 2026-09-30, or -3 (three days ago)
tng week             # last 7 days, plus a per-day strip
tng month            # last 30 days
tng --json week      # machine-readable output
tng --idle 15 week   # stricter: gaps over 15 min count as idle
tng scan             # index new activity and exit
tng watch            # keep indexing in the foreground
```

Every report scans for new activity before printing. Use `--no-scan` to skip that.

## Project layout

```
src/time_not_gone/
  sources.py   scanners for Claude Code and Cowork logs, workspace/worktree mapping
  store.py     SQLite schema and incremental read offsets
  report.py    turning timestamps into active time per workspace and day
  cli.py       the `tng` command
menubar/
  TimeNotGone.swift   SwiftUI MenuBarExtra app
  build.sh            builds and ad-hoc signs the .app bundle
```

The Python side uses only the standard library. The menubar app holds no data logic: it
displays the JSON that `tng menubar` prints.

## Limitations

- Only activity on this Mac is counted. Cloud and remote sessions and claude.ai chats
  aren't included.
- A tool call that runs longer than the idle cutoff without writing anything (a long
  build, for example) counts as idle.
- Desktop and Cowork store their data in undocumented places, so a future Claude update
  could change those paths or formats.
