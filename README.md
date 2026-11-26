<div align="center">

# T3 Session Cleaner

**Safely preview, select, and clean orphaned local sessions left behind after deleting threads in T3 Code** *(macOS, currently focused on Antigravity)*.

[![Release](https://img.shields.io/github/v/release/Jerry2003sky/t3-session-cleaner?label=release)](https://github.com/Jerry2003sky/t3-session-cleaner/releases)
[![macOS 15+](https://img.shields.io/badge/macOS-15%2B-111318?logo=apple&logoColor=white)](#quick-start)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-3776AB?logo=python&logoColor=white)](#quick-start)
[![Zero Dependencies](https://img.shields.io/badge/dependencies-zero-2ea44f)](#quick-start)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](./LICENSE)

[English](./README.md) · [简体中文](./README.zh-CN.md)

<br />

<img src="./assets/tui-en.png" alt="t3-session-cleaner interactive TUI dashboard" width="760" />

</div>

> [!NOTE]
> **Unofficial community tool** — not affiliated with T3 Code or Google Antigravity. Verified against T3 Code's `statev2.sqlite` schema; if an unknown schema appears (e.g. `statev3.sqlite`), the tool **refuses to run** rather than guessing.

## Quick Start

Requires **macOS 15+** and **Python 3.8+** — single file, zero third-party dependencies (uses built-in `/usr/bin/trash`, `lsof`, and `ps` only).

```bash
mkdir -p ~/.local/bin
curl -fsSL https://raw.githubusercontent.com/Jerry2003sky/t3-session-cleaner/main/t3-session-cleaner -o ~/.local/bin/t3-session-cleaner
chmod +x ~/.local/bin/t3-session-cleaner
```

Make sure `~/.local/bin` is in your `$PATH`, then run:

```bash
t3-session-cleaner
```

## Usage

### Interactive TUI

Running `t3-session-cleaner` in a terminal opens the dashboard, where you can inspect disk usage, run a standard or deep clean, prune stale index rows, or pick sessions individually:

<p align="center">
  <img src="./assets/tui-select-en.png" alt="t3-session-cleaner per-session selector" width="760" />
</p>

| Key | Action |
| --- | --- |
| `↑` / `↓` or `j` / `k` | Move cursor |
| `Space` | Toggle session selection (picker view) |
| `p` | Toggle `conversation_summaries.db` sync (when T3 is closed) |
| `Enter` | Confirm / open view |
| `r` | Rescan disk & T3 running status |
| `l` | Switch English / 中文 live |
| `Esc` / `q` | Back / quit |

### CLI Mode

```bash
t3-session-cleaner -n                         # Read-only preview
t3-session-cleaner -n -v                      # Preview including active protected sessions
t3-session-cleaner --apply                    # Clean deleted sessions older than 24h (to Trash)
t3-session-cleaner --apply --prune-summaries  # Also prune stale index rows (requires T3 closed)
t3-session-cleaner --lang en -n               # Override display language for one run
```

Run `t3-session-cleaner --help` for all options.

## Safety & Restore

- **Minimal scope** — only manages `conversations/`, `brain/`, `annotations/`, and (when T3 is closed) `conversation_summaries.db` under T3's Antigravity profile. Directories such as `implicit/` are never touched.
- **Conservative by default** — protects active/archived threads, nested subagents, in-flight handoffs/transfers, open file handles, and enforces a **24h cooldown**.
- **One-bundle Trash & restore** — moves cleaned files into a single timestamped folder in macOS Trash with `manifest.json` and a conflict-safe `restore.sh`.
- **No live DB writes** — `conversation_summaries.db` is never modified while T3 or `agy` is running; any index change backs up a WAL-merged snapshot + `restore.sh` to Trash first.

### Restoring a Cleaned Bundle

Drag `t3-session-cleaner-YYYY-MM-DD_HHMMSS` out of macOS Trash (e.g. to Desktop) and run:

```bash
bash ./t3-session-cleaner-YYYY-MM-DD_HHMMSS/restore.sh
```

`restore.sh` moves each session back to its exact original paths and skips an entire UUID group if any target conflicts.

> [!TIP]
> File restoration does not re-insert removed `conversation_summaries.db` rows. If the index was also synced or pruned, quit T3 Code first and run `restore.sh` inside the matching `t3-session-cleaner-summaries-backup-*` folder to merge missing rows back without losing newer ones.

<details>
<summary><strong>Reference: 5 Fail-Closed Safety Invariants &amp; Index Pruning Rules</strong></summary>

1. **Schema contract check** — refuses to run (exit code `2`) if a newer/unknown `statev*.sqlite` exists or any required `orchestration_v2_*` table or column is missing.
2. **Circuit breaker** — aborts if T3 reports live Antigravity threads and sessions exist on disk, yet zero match the active whitelist.
3. **Wide whitelist, narrow deletion** — protects active and archived threads, BFS-expanded nested native subagents, in-flight handoffs/transfers/outbox entries, `ANTIGRAVITY_CONVERSATION_ID`, open `lsof` handles, and a 24-hour cooldown window (computed from the later of T3 `deleted_at` and file `mtime`).
4. **TOCTOU re-check** — re-verifies T3 state, category, `mtime`, cooldown, and open handles immediately before touching disk.
5. **Restorable Trash bundle &amp; safe DB syncing**:
   - **Files**: staged and moved as one timestamped bundle via `/usr/bin/trash` with `manifest.json` and a non-overwriting UUID-grouped `restore.sh` (auto-rolls back on failure; preserves the staging bundle with `restore.sh` if rollback fails).
   - **Index safety**: `conversation_summaries.db` is never touched while T3 (`statev2.sqlite`) or `agy` is running or while `.t3-clean-staging` leftovers exist. CLI mode requires explicit `--prune-summaries`; TUI session cleanups automatically sync only the sessions cleaned in that run when T3 is completely closed (toggleable via `p`; pre-existing stale rows stay for the dedicated **Prune index** action). Before any `DELETE`, the tool verifies the single-table / no-trigger / no-FK contract and backs up a consistent WAL-merged snapshot (+ merge-safe `restore.sh`, which also refuses to run while T3 or `agy` has either DB open) to Trash.
   - **Index pruning scope**: a row is pruned only when inactive, unbacked by files on disk, **and** matching at least one of:
     - marked deleted or superseded in T3 (including recursively expanded subagents of deleted threads);
     - an empty shell row (`step_count = 0` and empty `title`, e.g. `t3-antigravity-setup-*` probe entries);
     - a session successfully cleaned during the current run.

</details>

<details>
<summary><strong>Reference: Paths &amp; Exit Codes</strong></summary>

**Paths**

- Managed data root: `~/.t3/userdata/providers/antigravity/` (`conversations/`, `brain/`, `annotations/`, `conversation_summaries.db`)
- Saved language preference: `~/.config/t3-session-cleaner/config.json` (auto-detected from system locale on first run)

**Exit Codes**

| Code | Meaning |
| --- | --- |
| `0` | Success (or user cancelled cleanly) |
| `1` | Runtime error |
| `2` | Fail-closed safety / schema contract violation |

</details>

## Development & License

```bash
python3 -m unittest discover -s tests -v
```

All tests run in isolated temporary directories with a mock `trash` binary and never touch `~/.t3`, `~/.config`, or macOS Trash.

Licensed under the [MIT License](./LICENSE).
