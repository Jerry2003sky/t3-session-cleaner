<div align="center">

# T3 Session Cleaner

**Safely preview, interactively select, and clean orphaned local sessions left behind by T3 Code on macOS (currently focused on Antigravity).**

[![Release](https://img.shields.io/github/v/release/Jerry2003sky/t3-session-cleaner?label=release)](https://github.com/Jerry2003sky/t3-session-cleaner/releases)
[![macOS 15+](https://img.shields.io/badge/macOS-15%2B-111318?logo=apple&logoColor=white)](#requirements)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-3776AB?logo=python&logoColor=white)](#requirements)
[![Zero Dependencies](https://img.shields.io/badge/dependencies-zero-2ea44f)](#requirements)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](./LICENSE)

[English](./README.md) · [简体中文](./README.zh-CN.md)

<br />

<img src="./assets/tui-en.png" alt="t3-session-cleaner interactive TUI dashboard" width="760" />

</div>

> **Unofficial community tool** — not affiliated with T3 Code or Google Antigravity. Verified against T3 Code's `statev2.sqlite` schema on macOS 15+. If T3 Code changes its storage schema (e.g. `statev3.sqlite`), the tool **refuses to run** rather than guessing.

---

## Why?

When you delete a conversation in T3 Code, T3 soft-deletes the thread in `statev2.sqlite` and eventually compacts old events, while Antigravity's local session files (`conversations/<uuid>.db`, `brain/<uuid>/`, `annotations/<uuid>.pbtxt`) and `conversation_summaries.db` rows under `~/.t3/userdata/providers/antigravity/` accumulate on disk over time.

`t3-session-cleaner` is a **single-file, zero-dependency** Python 3 CLI/TUI designed to inspect and reclaim that space with fail-closed safety guarantees.

## Highlights

- **Single file, zero dependencies** — pure Python 3 standard library + built-in macOS tools (`/usr/bin/trash`, `lsof`, `ps`).
- **Interactive TUI & scriptable CLI** — visual storage breakdown, per-session multi-select picker, auto-refreshing dashboard after each cleanup, live rescan (`r`), and one-key English / 中文 toggle (`l`).
- **Fail-closed safety model** — refuses to run on unknown T3 database schemas, protects active/archived/subagent/in-flight sessions, enforces a 24h cooldown, and re-checks state right before touching disk.
- **One-click reversible Trash bundle** — moves cleaned files into a single dated folder in macOS Trash along with `manifest.json` and a conflict-safe `restore.sh`.
- **Safe index syncing** — automatically keeps `conversation_summaries.db` in sync for cleaned sessions when T3 & `agy` are closed (toggleable via `p`; opt-in `--prune-summaries` in CLI), always backing up a WAL-merged snapshot + merge-safe `restore.sh` to Trash first.

## Quick Install

```bash
mkdir -p ~/.local/bin
curl -fsSL https://raw.githubusercontent.com/Jerry2003sky/t3-session-cleaner/main/t3-session-cleaner -o ~/.local/bin/t3-session-cleaner
chmod +x ~/.local/bin/t3-session-cleaner
```

Ensure `~/.local/bin` is in your `$PATH`, then run:

```bash
t3-session-cleaner
```

## Usage

### Interactive TUI (default in a terminal)

Running `t3-session-cleaner` inside a TTY opens the interactive dashboard, where you can trigger a standard clean, deep clean, index prune, or drop into the per-session selector:

<p align="center">
  <img src="./assets/tui-select-en.png" alt="t3-session-cleaner per-session selector" width="760" />
</p>

| Key | Action |
| --- | --- |
| `↑` / `↓` or `j` / `k` | Move cursor |
| `Space` | Toggle session selection (in manual picker) |
| `p` | Toggle syncing `conversation_summaries.db` (in manual picker / confirmation screen when T3 is closed) |
| `Enter` | Confirm / open view |
| `r` | Live rescan disk & T3 running status (e.g. right after quitting T3 to prune the index) |
| `l` | Toggle English / 中文 live (saved to `~/.config/t3-session-cleaner/config.json`; auto-detected from locale on first run) |
| `Esc` / `q` | Go back / quit |

### CLI Mode

```bash
# Read-only preview
t3-session-cleaner -n

# Preview including all active protected sessions
t3-session-cleaner -n -v

# Clean deleted sessions older than 24h (moves one bundle to macOS Trash)
t3-session-cleaner --apply

# Also prune stale index rows in conversation_summaries.db (requires T3 closed)
t3-session-cleaner --apply --prune-summaries

# Override display language for a single run
t3-session-cleaner --lang en -n
```

Run `t3-session-cleaner --help` for all flags.

## Managed Scope

`t3-session-cleaner` only inspects and manages `conversations/`, `brain/`, `annotations/`, and (when T3 is closed) `conversation_summaries.db` inside T3's Antigravity profile directory. Other directories such as `implicit/` are **never touched**.

## Safety Invariants (Fail-Closed)

1. **Schema contract check**: Refuses to run (exit code `2`) if a newer/unknown `statev*.sqlite` exists or any required `orchestration_v2_*` table/column is missing.
2. **Circuit breaker**: Aborts if T3 reports live Antigravity threads and sessions exist on disk, yet zero match the active whitelist.
3. **Wide whitelist, narrow deletion**: Protects active + archived threads, BFS-expanded nested native subagents, in-flight handoffs/transfers/outbox entries, `ANTIGRAVITY_CONVERSATION_ID`, open `lsof` handles, and a 24-hour cooldown window (computed from the later of T3 `deleted_at` and file `mtime`).
4. **TOCTOU re-check**: Re-verifies T3 state, category, `mtime`, cooldown, and open handles right before touching disk.
5. **Restorable Trash bundle & safe DB syncing**:
   - File cleanup moves session files into **one dated folder** in macOS Trash via `/usr/bin/trash`, bundled with `manifest.json` and a non-overwriting UUID-grouped `restore.sh` (rolls back on failure; preserves staging bundle with `restore.sh` if any rollback fails).
   - `conversation_summaries.db` is **never touched while T3 (`statev2.sqlite`) or `agy` is running** or when `.t3-clean-staging` leftovers exist. In CLI mode (`--apply`), DB pruning is strictly opt-in (`--prune-summaries`); in interactive TUI mode, when T3 is completely closed and no staging leftover exists, session cleanups automatically remove only the matching rows for the sessions cleaned in that run (explicitly displayed and toggleable with `p` on the confirmation screen; pre-existing stale rows stay untouched for the dedicated **Prune index** action). Before any `DELETE` + `VACUUM`, it verifies the single-table/no-trigger/no-FK contract and backs up a consistent WAL-merged snapshot (+ merge-safe `restore.sh`) to Trash.
   - **Index pruning scope**: A row in `conversation_summaries.db` is pruned only if it is not active, has no backing files on disk, **and** satisfies at least one of: (a) marked deleted/superseded in T3 (including recursively expanded subagents of deleted threads), (b) an empty shell row (`step_count = 0` and empty `title`, e.g. T3 `t3-antigravity-setup-*` probe entries), or (c) its session was successfully cleaned during this run.

## Restoring a Cleaned Bundle

Open macOS Trash, drag the `t3-session-cleaner-YYYY-MM-DD_HHMMSS` folder out (e.g. to Desktop), and run:

```bash
bash ./t3-session-cleaner-YYYY-MM-DD_HHMMSS/restore.sh
```

`restore.sh` moves each session back to its exact original paths and skips an entire UUID group if any target file conflicts.

> **Note**: Restoring a session bundle from Trash restores the session files on disk, but does **not** automatically re-insert rows removed from `conversation_summaries.db`. If the index was also synced or pruned, run `restore.sh` inside the corresponding `t3-session-cleaner-summaries-backup-*` folder to merge missing rows back without losing newer rows.

## Requirements

- **macOS 15+ (Sequoia or newer)** with built-in `/usr/bin/trash`, `lsof`, and `ps`
- **Python 3.8+** (standard library only; no `pip` packages)

## Exit Codes

| Code | Meaning |
| --- | --- |
| `0` | Success (or user cancelled cleanly) |
| `1` | Runtime error |
| `2` | Fail-closed safety/schema contract violation |

## Development & Tests

```bash
python3 -m unittest discover -s tests -v
```

All tests run in isolated temporary directories with a stub trash binary and never touch `~/.t3`, `~/.config`, or macOS Trash.

## License

[MIT](./LICENSE)
