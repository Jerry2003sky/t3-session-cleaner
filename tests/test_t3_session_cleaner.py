"""Isolated unit and PTY integration tests for t3-session-cleaner.

All tests run inside tempfile.TemporaryDirectory() with isolated XDG_CONFIG_HOME
and T3_CLEAN_TRASH_BIN — never touching ~/.t3, ~/.config, or macOS Trash.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import importlib.machinery
import importlib.util
import io
import json
import os
import pty
import re
import select
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unicodedata
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "t3-session-cleaner"
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def load_module():
    loader = importlib.machinery.SourceFileLoader("t3_session_cleaner_mod", str(SCRIPT))
    spec = importlib.util.spec_from_loader("t3_session_cleaner_mod", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["t3_session_cleaner_mod"] = mod
    loader.exec_module(mod)
    return mod


mod = load_module()


def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


def disp_w(s: str) -> int:
    return sum(
        0 if unicodedata.combining(c) else (2 if unicodedata.east_asian_width(c) in ("F", "W") else 1)
        for c in strip_ansi(s)
    )


def iso_utc_ago(hours: float) -> str:
    t = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def make_stub_trash(bin_dir: Path, trash_dir: Path, *, fail: bool = False, name: str = "") -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    trash_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / (name or ("trash_fail" if fail else "trash_stub"))
    if fail:
        stub.write_text("#!/usr/bin/env python3\nimport sys\nsys.stderr.write('simulated failure\\n')\nsys.exit(1)\n")
    else:
        stub.write_text(
            "#!/usr/bin/env python3\n"
            "import shutil, sys\n"
            "from pathlib import Path\n"
            f"dst_root = Path({str(trash_dir)!r})\n"
            "for a in sys.argv[1:]:\n"
            "    if a.startswith('-'): continue\n"
            "    p = Path(a)\n"
            "    dst = dst_root / p.name\n"
            "    i = 1\n"
            "    while dst.exists():\n"
            "        dst = dst_root / f'{p.name}-{i}'\n"
            "        i += 1\n"
            "    shutil.move(str(p), str(dst))\n"
        )
    stub.chmod(0o755)
    return stub


def make_fixture(
    root: Path,
    *,
    active_sz: int = 2048,
    del_sizes: tuple[int, ...] = (4096, 2048),
    del_age_hours: float = 72.0,
    del_deleted_at: str | None = None,
    orphan_ids: tuple[str, ...] = (),
    leftover_staging: bool = False,
    multi_profile: bool = False,
    with_subagent: bool = False,
):
    userdata = root / "userdata"
    userdata.mkdir(parents=True, exist_ok=True)
    db = userdata / "statev2.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE orchestration_v2_projection_threads (
            thread_id TEXT, title TEXT, default_provider TEXT,
            provider_instance_id TEXT, archived_at TEXT, deleted_at TEXT
        );
        CREATE TABLE orchestration_v2_projection_provider_threads (
            thread_id TEXT, provider TEXT, provider_thread_id TEXT, payload_json TEXT
        );
        CREATE TABLE orchestration_v2_projection_run_attempts (
            thread_id TEXT, provider TEXT, payload_json TEXT
        );
        CREATE TABLE orchestration_v2_projection_subagents (
            thread_id TEXT, provider TEXT, payload_json TEXT
        );
        CREATE TABLE orchestration_v2_projection_context_handoffs (
            thread_id TEXT, status TEXT, payload_json TEXT
        );
        CREATE TABLE orchestration_v2_projection_context_transfers (
            source_thread_id TEXT, target_thread_id TEXT, status TEXT, payload_json TEXT
        );
        CREATE TABLE orchestration_v2_effect_outbox (
            thread_id TEXT, status TEXT, payload_json TEXT
        );
        """
    )
    u_act = "11111111-1111-1111-1111-111111111111"
    u_sub = "11111111-1111-1111-1111-222222222222"
    conn.execute(
        "INSERT INTO orchestration_v2_projection_threads VALUES (?,?,?,?,?,?)",
        ("t-act", "Active Thread", "antigravity", "antigravity", None, None),
    )
    conn.execute(
        "INSERT INTO orchestration_v2_projection_provider_threads VALUES (?,?,?,?)",
        ("t-act", "antigravity", f"native-thread:{u_act}", json.dumps({"nativeThreadRef": {"driver": "antigravity", "nativeId": u_act}})),
    )

    del_ts_str = del_deleted_at if del_deleted_at is not None else iso_utc_ago(del_age_hours)
    del_uuids = []
    for idx, sz in enumerate(del_sizes):
        u = f"22222222-2222-2222-2222-{idx + 1:012d}"
        del_uuids.append((u, sz))
        conn.execute(
            "INSERT INTO orchestration_v2_projection_threads VALUES (?,?,?,?,?,?)",
            (f"t-del-{idx}", f"Deleted {idx + 1}", "antigravity", "antigravity", None, del_ts_str),
        )
        conn.execute(
            "INSERT INTO orchestration_v2_projection_provider_threads VALUES (?,?,?,?)",
            (f"t-del-{idx}", "antigravity", f"native-thread:{u}", json.dumps({"nativeThreadRef": {"driver": "antigravity", "nativeId": u}})),
        )
    conn.commit()
    conn.close()

    profiles = ["aaaa1111bbbb2222"] + (["cccc3333dddd4444"] if multi_profile else [])
    old_ts = time.time() - del_age_hours * 3600
    acp_dirs = []
    for p_idx, pname in enumerate(profiles):
        acp = userdata / "providers" / "antigravity" / pname / "antigravity-acp"
        (acp / "conversations").mkdir(parents=True)
        (acp / "brain").mkdir(parents=True)
        acp_dirs.append(acp)

        (acp / "conversations" / f"{u_act}.db").write_bytes(b"A" * active_sz)
        if with_subagent and p_idx == 0:
            (acp / "conversations" / f"{u_sub}.db").write_bytes(b"S" * 1024)
            sub_meta = acp / "brain" / u_act / ".system_generated" / "subagents"
            sub_meta.mkdir(parents=True)
            (sub_meta / f"{u_sub}.json").write_text(json.dumps({"conversationId": u_sub}))

        sconn = sqlite3.connect(acp / "conversation_summaries.db")
        sconn.execute("PRAGMA journal_mode=WAL")
        sconn.execute(
            "CREATE TABLE conversation_summaries "
            "(conversation_id TEXT PRIMARY KEY, title TEXT, step_count INT, last_modified_time TEXT, parent_conversation_id TEXT)"
        )
        sconn.execute("INSERT INTO conversation_summaries VALUES (?,?,?,?,?)", (u_act, "Active", 5, "2026-11-25", ""))
        if with_subagent and p_idx == 0:
            sconn.execute("INSERT INTO conversation_summaries VALUES (?,?,?,?,?)", (u_sub, "Subagent", 2, "2026-11-25", u_act))
        if p_idx == 0:
            for u, sz in del_uuids:
                df = acp / "conversations" / f"{u}.db"
                df.write_bytes(b"D" * sz)
                os.utime(df, (old_ts, old_ts))
                sconn.execute("INSERT INTO conversation_summaries VALUES (?,?,?,?,?)", (u, f"Del {u[-2:]}", 3, "2026-11-20", ""))
        for oi in orphan_ids:
            # 0-step empty shell row (matches Q01 rule #2)
            sconn.execute("INSERT INTO conversation_summaries VALUES (?,?,?,?,?)", (oi, "", 0, "0001-01-01 00:00:00", ""))
        sconn.commit()
        sconn.close()

        if leftover_staging and p_idx == 0:
            st_dir = acp.parent / ".t3-clean-staging" / "interrupted-bundle"
            st_dir.mkdir(parents=True)
            (st_dir / "marker").write_text("x")

    return acp_dirs, [u for u, _ in del_uuids]


def run_pty(cmd, env, cols=92, rows=26, steps=()):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    proc = subprocess.Popen(cmd, stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True)
    os.close(slave)

    def drain(wait=0.25, expect=None, timeout=5.0):
        out = bytearray()
        deadline = time.time() + (timeout if expect else wait)
        matched = False
        while time.time() < deadline:
            if not select.select([master], [], [], max(0.02, deadline - time.time()))[0]:
                break
            try:
                chunk = os.read(master, 65536)
                if not chunk:
                    break
                out.extend(chunk)
                if expect:
                    if not matched and expect in strip_ansi(out.decode("utf-8", errors="replace")):
                        matched = True
                        deadline = min(deadline, time.time() + 0.12)
                    elif matched:
                        deadline = min(deadline, time.time() + 0.06)
                else:
                    deadline = min(deadline, time.time() + 0.08)
            except OSError:
                break
        return out.decode("utf-8", errors="replace")

    captures = [drain(0.6)]
    for action in steps:
        expect = None
        if callable(action):
            action(proc, master)
        elif isinstance(action, tuple):
            data, expect = action
            try:
                os.write(master, data)
            except OSError:
                break
        else:
            try:
                os.write(master, action)
            except OSError:
                break
        captures.append(drain(0.45, expect=expect))

    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    captures.append(drain(0.15))
    os.close(master)
    return proc.returncode, captures


class TestT3SessionCleaner(unittest.TestCase):
    def setUp(self):
        # Running inside an Antigravity agent terminal must not leak its session into the fixtures.
        env_patch = mock.patch.dict(os.environ)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        os.environ.pop("ANTIGRAVITY_CONVERSATION_ID", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.trash_dir = self.root / "trash"
        self.trash_stub = make_stub_trash(self.root / "bin", self.trash_dir)
        self.cfg_dir = self.root / "xdg_cfg"
        self.env = {
            **os.environ,
            "XDG_CONFIG_HOME": str(self.cfg_dir),
            "T3_CLEAN_TRASH_BIN": str(self.trash_stub),
            "LANG": "en_US.UTF-8",
        }
        self.env.pop("LC_ALL", None)
        self.env.pop("LC_MESSAGES", None)

    def tearDown(self):
        self.tmp.cleanup()

    def assertBoxAligned(self, screen_text: str, max_cols: int, label: str = ""):
        frame = screen_text.split("\x1b[H")[-1]
        lines = [strip_ansi(l.replace("\r", "")) for l in frame.split("\n")]
        box_lines = [l for l in lines if l.strip().startswith(("╭", "│", "╰"))]
        self.assertTrue(box_lines, f"[{label}] No box lines found")
        widths = {disp_w(l) for l in box_lines}
        self.assertEqual(len(widths), 1, f"[{label}] Misaligned widths {widths}:\n" + "\n".join(box_lines))
        self.assertLessEqual(next(iter(widths)), max_cols, f"[{label}] Box exceeds {max_cols}")
        for l in lines:
            if l.strip():
                self.assertLessEqual(disp_w(l), max_cols, f"[{label}] Overflow line: {l!r}")

    # ------------------------- Baseline tests -------------------------

    def test_version_and_auto_locale_detection(self):
        res = subprocess.run([str(SCRIPT), "--version"], env=self.env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertIn(mod.__version__, res.stdout)

        t = self.root / "t_lang"
        make_fixture(t, del_sizes=())
        en_run = subprocess.run([str(SCRIPT), "--t3-home", str(t), "-n", "--skip-lsof"], env=self.env, capture_output=True, text=True)
        self.assertIn("Dry-run", en_run.stdout)

        zh_env = {**self.env, "LANG": "zh_CN.UTF-8"}
        zh_run = subprocess.run([str(SCRIPT), "--t3-home", str(t), "-n", "--skip-lsof"], env=zh_env, capture_output=True, text=True)
        self.assertIn("预览模式", zh_run.stdout)

    def test_fail_closed_contracts_and_circuit_breaker(self):
        t = self.root / "t_contract"
        make_fixture(t)
        (t / "userdata" / "statev3.sqlite").write_bytes(b"")
        res = subprocess.run([str(SCRIPT), "--t3-home", str(t), "-n", "--skip-lsof"], env=self.env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 2)
        (t / "userdata" / "statev3.sqlite").unlink()

        _, reps = mod.build_plan(t, include_superseded=False, include_unknown=False, min_age_hours=24, skip_lsof=True)
        (t / "userdata" / "statev3.sqlite").write_bytes(b"")
        rc = mod.execute_cleanup(t, reps, [], prune_summaries=True, skip_lsof=True, st=mod.Style(False), tr=mod.Translator("en"))
        self.assertEqual(rc, 2)
        (t / "userdata" / "statev3.sqlite").unlink()

        t_cb = self.root / "t_cb"
        acps_cb, _ = make_fixture(t_cb, del_sizes=(4096,))
        (acps_cb[0] / "conversations" / "11111111-1111-1111-1111-111111111111.db").unlink()
        with self.assertRaises(mod.ContractError):
            mod.build_plan(t_cb, include_superseded=False, include_unknown=False, min_age_hours=24, skip_lsof=True)

        # Run by an agent inside T3: the env session stays protected but must not satisfy the breaker on its own.
        t_env = self.root / "t_cb_env"
        u_act = "11111111-1111-1111-1111-111111111111"
        make_fixture(t_env, del_sizes=(4096,))
        with mock.patch.dict(os.environ, {"ANTIGRAVITY_CONVERSATION_ID": u_act}):
            _, reps_env = mod.build_plan(t_env, include_superseded=False, include_unknown=False, min_age_hours=24, skip_lsof=True)
            self.assertEqual({s.uuid: s.category for s in reps_env[0].sessions}[u_act], "ACTIVE")
            drift = "99999999-9999-9999-9999-999999999999"
            c_env = sqlite3.connect(t_env / "userdata" / "statev2.sqlite")
            c_env.execute(
                "UPDATE orchestration_v2_projection_provider_threads SET provider_thread_id=?, payload_json=? WHERE thread_id='t-act'",
                (f"native-thread:{drift}", json.dumps({"nativeThreadRef": {"driver": "antigravity", "nativeId": drift}})),
            )
            c_env.commit()
            c_env.close()
            with self.assertRaises(mod.ContractError):
                mod.build_plan(t_env, include_superseded=False, include_unknown=False, min_age_hours=24, skip_lsof=True)

    def test_subagent_protection_and_orphan_counts(self):
        t = self.root / "t_sub"
        orphans = (
            "33333333-3333-3333-3333-000000000001",
            "33333333-3333-3333-3333-000000000002",
        )
        make_fixture(t, del_sizes=(4096, 2048), orphan_ids=orphans, leftover_staging=False, with_subagent=True)
        _, reps = mod.build_plan(t, include_superseded=False, include_unknown=False, min_age_hours=24, skip_lsof=True)
        by_id = {s.uuid: s for s in reps[0].sessions}
        self.assertEqual(by_id["11111111-1111-1111-1111-222222222222"].category, "ACTIVE")
        self.assertEqual(len(reps[0].orphan_summary_ids), 2)
        chosen = [s for s in reps[0].sessions if s.selected]
        self.assertEqual(len(chosen), 2)
        self.assertEqual(mod.estimate_pruned_summaries(reps, []), 2)
        self.assertEqual(mod.estimate_pruned_summaries(reps, chosen), 4)
        self.assertEqual(mod.estimate_pruned_summaries(reps, chosen, only_cleaned=True), 2)
        self.assertEqual(mod.estimate_pruned_summaries(reps, [], only_cleaned=True), 0)

    def test_bundled_trash_restore_and_atomic_rollback(self):
        t = self.root / "t_trash"
        acps, del_ids = make_fixture(t, del_sizes=(4096,))
        target_db = acps[0] / "conversations" / f"{del_ids[0]}.db"
        # Real sessions also own brain/<uuid>/ and annotations/<uuid>.pbtxt; they must leave and return together.
        brain_file = acps[0] / "brain" / del_ids[0] / "task.md"
        brain_file.parent.mkdir(parents=True)
        brain_file.write_text("notes")
        ann_file = acps[0] / "annotations" / f"{del_ids[0]}.pbtxt"
        ann_file.parent.mkdir(parents=True)
        ann_file.write_text("ann")
        old = time.time() - 48 * 3600
        for p in (brain_file, brain_file.parent, ann_file):
            os.utime(p, (old, old))

        fail_stub = make_stub_trash(self.root / "bin", self.trash_dir, fail=True)
        fail_env = {**self.env, "T3_CLEAN_TRASH_BIN": str(fail_stub)}
        r_fail = subprocess.run([str(SCRIPT), "--t3-home", str(t), "--apply", "-y", "--skip-lsof"], env=fail_env, capture_output=True, text=True)
        self.assertEqual(r_fail.returncode, 1)
        self.assertTrue(target_db.exists(), "Failed trash must restore original file")

        r_ok = subprocess.run([str(SCRIPT), "--t3-home", str(t), "--apply", "-y"], env=self.env, capture_output=True, text=True)
        self.assertEqual(r_ok.returncode, 0, r_ok.stdout + r_ok.stderr)
        self.assertFalse(target_db.exists())
        self.assertFalse(brain_file.parent.exists())
        self.assertFalse(ann_file.exists())
        bundles = list(self.trash_dir.glob("t3-session-cleaner-2*"))
        self.assertEqual(len(bundles), 1)
        self.assertTrue((bundles[0] / "manifest.json").is_file())
        r_rest = subprocess.run(["bash", str(bundles[0] / "restore.sh")], capture_output=True, text=True)
        self.assertEqual(r_rest.returncode, 0)
        self.assertTrue(target_db.exists())
        self.assertEqual(brain_file.read_text(), "notes")
        self.assertEqual(ann_file.read_text(), "ann")

    def test_summaries_contract_wal_backup_and_multiprofile(self):
        t = self.root / "t_sum"
        orphans = ("33333333-3333-3333-3333-000000000001",)
        acps, _ = make_fixture(t, del_sizes=(), orphan_ids=orphans, multi_profile=True)

        conn = sqlite3.connect(acps[1] / "conversation_summaries.db")
        conn.execute("CREATE TABLE unknown_future_table (id INT)")
        conn.commit()
        conn.close()

        r_idx = subprocess.run(
            [str(SCRIPT), "--t3-home", str(t), "--apply", "-y", "--prune-summaries", "--skip-lsof"],
            env=self.env, capture_output=True, text=True,
        )
        self.assertEqual(r_idx.returncode, 0, r_idx.stdout + r_idx.stderr)
        self.assertIn("unexpected tables", r_idx.stdout)
        self.assertNotIn("0 session(s)", r_idx.stdout)

        backups = list(self.trash_dir.glob("t3-session-cleaner-summaries-backup-*-aaaa1111"))
        self.assertEqual(len(backups), 1)
        c0 = sqlite3.connect(acps[0] / "conversation_summaries.db")
        self.assertEqual(c0.execute("SELECT COUNT(*) FROM conversation_summaries").fetchone()[0], 1)
        c0.close()
        r_rest = subprocess.run(["bash", str(backups[0] / "restore.sh")], capture_output=True, text=True)
        self.assertEqual(r_rest.returncode, 0, r_rest.stdout + r_rest.stderr)
        c0 = sqlite3.connect(acps[0] / "conversation_summaries.db")
        self.assertEqual(c0.execute("SELECT COUNT(*) FROM conversation_summaries").fetchone()[0], 2)
        c0.close()

    def test_tui_csi_immunity_sigwinch_refresh_and_alignment(self):
        t = self.root / "t_tui"
        orphans = ("33333333-3333-3333-3333-000000000001",)
        make_fixture(t, active_sz=10 * 1024 * 1024, del_sizes=(10 * 1024,), orphan_ids=orphans)

        def resize_to_74(proc, master):
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 74, 0, 0))
            os.kill(proc.pid, signal.SIGWINCH)

        rc, caps = run_pty(
            [str(SCRIPT), "--t3-home", str(t), "--skip-lsof", "--lang", "zh"],
            self.env,
            cols=100,
            rows=26,
            steps=[
                resize_to_74,
                b"\x1b[B\x1b[B\x1b[B\r",
                b"n",
                b"\x1b[1;5A\x1b[3~\x1b[<64;5;5M\xe4\xbd\xa0",
                b"\x1b",
                b"\x1b[A\x1b[A\x1b[A\r",
                b"l",
                b"\x1b",
                b"r",
                b"q",
            ],
        )
        self.assertEqual(rc, 0)
        s0 = strip_ansi(caps[0])
        self.assertIn("<1%", s0)
        self.assertIn(">99%", s0)
        self.assertBoxAligned(caps[0], 100, "init-100-zh")
        self.assertBoxAligned(caps[1], 74, "winch-74-zh")
        s_csi = strip_ansi(caps[4].split("\x1b[H")[-1])
        self.assertIn("自定义勾选", s_csi)
        self.assertIn("已选 0 项", s_csi)
        self.assertBoxAligned(caps[6], 74, "confirm-74-zh")
        self.assertBoxAligned(caps[7], 74, "confirm-74-en")
        self.assertIn("Confirm Cleanup", strip_ansi(caps[7].split("\x1b[H")[-1]))
        self.assertIn("Rescanned", strip_ansi(caps[9].split("\x1b[H")[-1]))

    # ------------------------- Regression tests (R01–R18, Q01) -------------------------

    def test_r01_rollback_failure_preserves_staging_and_restores(self):
        # Case (a): target file recreated before trash fails -> original preserved in staging
        t_a = self.root / "t_r01a"
        acps_a, del_a = make_fixture(t_a, del_sizes=(4096,))
        orig_a = acps_a[0] / "conversations" / f"{del_a[0]}.db"
        orig_a.write_bytes(b"ORIGINAL_DATA_A")
        old_ts = time.time() - 72 * 3600
        os.utime(orig_a, (old_ts, old_ts))

        stub_a = self.root / "bin" / "trash_recreate"
        stub_a.parent.mkdir(parents=True, exist_ok=True)
        stub_a.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "from pathlib import Path\n"
            f"Path({str(orig_a)!r}).write_bytes(b'NEW_CONFLICT')\n"
            "sys.exit(1)\n"
        )
        stub_a.chmod(0o755)
        r_a = subprocess.run(
            [str(SCRIPT), "--t3-home", str(t_a), "--apply", "-y", "--skip-lsof"],
            env={**self.env, "T3_CLEAN_TRASH_BIN": str(stub_a)},
            capture_output=True, text=True,
        )
        self.assertEqual(r_a.returncode, 1)
        combined_a = r_a.stdout + r_a.stderr
        self.assertNotIn("rolled back", combined_a.lower())
        staging_a = acps_a[0].parent / ".t3-clean-staging"
        self.assertIn(str(staging_a), combined_a)
        staged_bundles_a = list(staging_a.glob("t3-session-cleaner-*"))
        self.assertEqual(len(staged_bundles_a), 1)
        preserved_a = staged_bundles_a[0] / "conversations" / f"{del_a[0]}.db"
        self.assertTrue(preserved_a.is_file())
        self.assertEqual(preserved_a.read_bytes(), b"ORIGINAL_DATA_A")

        # Case (b): conversations/ becomes read-only before trash fails -> preserved, restore.sh works after chmod
        t_b = self.root / "t_r01b"
        acps_b, del_b = make_fixture(t_b, del_sizes=(4096,))
        conv_dir = acps_b[0] / "conversations"
        orig_b = conv_dir / f"{del_b[0]}.db"
        orig_b.write_bytes(b"ORIGINAL_DATA_B")
        os.utime(orig_b, (old_ts, old_ts))

        stub_b = self.root / "bin" / "trash_readonly"
        stub_b.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            f"os.chmod({str(conv_dir)!r}, 0o555)\n"
            "sys.exit(1)\n"
        )
        stub_b.chmod(0o755)
        try:
            r_b = subprocess.run(
                [str(SCRIPT), "--t3-home", str(t_b), "--apply", "-y", "--skip-lsof"],
                env={**self.env, "T3_CLEAN_TRASH_BIN": str(stub_b)},
                capture_output=True, text=True,
            )
            self.assertEqual(r_b.returncode, 1)
            combined_b = r_b.stdout + r_b.stderr
            self.assertNotIn("rolled back", combined_b.lower())
            staging_b = acps_b[0].parent / ".t3-clean-staging"
            self.assertIn(str(staging_b), combined_b)
            bundles_b = list(staging_b.glob("t3-session-cleaner-*"))
            self.assertEqual(len(bundles_b), 1)
            preserved_b = bundles_b[0] / "conversations" / f"{del_b[0]}.db"
            self.assertTrue(preserved_b.is_file())
            self.assertEqual(preserved_b.read_bytes(), b"ORIGINAL_DATA_B")
        finally:
            os.chmod(conv_dir, 0o755)

        # Restore permissions done -> running preserved restore.sh restores original file
        r_rest = subprocess.run(["bash", str(bundles_b[0] / "restore.sh")], capture_output=True, text=True)
        self.assertEqual(r_rest.returncode, 0, r_rest.stdout + r_rest.stderr)
        self.assertTrue(orig_b.is_file())
        self.assertEqual(orig_b.read_bytes(), b"ORIGINAL_DATA_B")

    def test_r02_dry_run_conflicts_with_apply_and_interactive(self):
        t = self.root / "t_r02"
        acps, del_ids = make_fixture(t, del_sizes=(4096,))
        target = acps[0] / "conversations" / f"{del_ids[0]}.db"

        r1 = subprocess.run([str(SCRIPT), "--t3-home", str(t), "-n", "--apply", "-y", "--skip-lsof"], env=self.env, capture_output=True, text=True)
        self.assertNotEqual(r1.returncode, 0)
        self.assertTrue(target.exists())

        r2 = subprocess.run([str(SCRIPT), "--t3-home", str(t), "-i", "-n", "--skip-lsof"], env=self.env, capture_output=True, text=True)
        self.assertNotEqual(r2.returncode, 0)
        self.assertTrue(target.exists())

    def test_r03_cooldown_uses_deleted_at_and_latest_for_multi_parent(self):
        t = self.root / "t_r03"
        acps, _ = make_fixture(t, del_sizes=())
        acp = acps[0]
        db = t / "userdata" / "statev2.sqlite"
        conn = sqlite3.connect(db)

        now_iso = iso_utc_ago(0.1)
        old_iso = iso_utc_ago(72.0)
        old_mtime = time.time() - 72 * 3600
        new_mtime = time.time() - 60

        u_recent_del = "22222222-2222-2222-2222-000000000011"  # old mtime, just deleted -> cooldown
        u_recent_mt = "22222222-2222-2222-2222-000000000012"   # old deleted_at, new mtime -> cooldown
        u_both_old = "22222222-2222-2222-2222-000000000013"    # both old -> cleanable
        u_bad_date = "22222222-2222-2222-2222-000000000014"    # unparseable deleted_at -> cooldown unless min-age 0
        u_multi_thr = "22222222-2222-2222-2222-000000000015"   # referenced by old & new deleted threads -> latest wins
        u_sub_shared = "22222222-2222-2222-2222-000000000016"  # subagent reachable from both old & new deleted parents -> latest wins
        u_sub_old = "22222222-2222-2222-2222-000000000017"     # subagent of old deleted parent only -> cleanable

        rows = [
            ("td-1", "Recent Del", now_iso, u_recent_del),
            ("td-2", "Recent Mtime", old_iso, u_recent_mt),
            ("td-3", "Both Old", old_iso, u_both_old),
            ("td-4", "Bad Date", "not-a-timestamp", u_bad_date),
            ("td-5a", "Multi Old", old_iso, u_multi_thr),
            ("td-5b", "Multi New", now_iso, u_multi_thr),
        ]
        for tid, title, d_at, uid in rows:
            conn.execute("INSERT INTO orchestration_v2_projection_threads VALUES (?,?,?,?,?,?)", (tid, title, "antigravity", "antigravity", None, d_at))
            conn.execute("INSERT INTO orchestration_v2_projection_provider_threads VALUES (?,?,?,?)", (tid, "antigravity", f"native-thread:{uid}", json.dumps({"nativeThreadRef": {"driver": "antigravity", "nativeId": uid}})))
        conn.commit()
        conn.close()

        for uid, mt in (
            (u_recent_del, old_mtime),
            (u_recent_mt, new_mtime),
            (u_both_old, old_mtime),
            (u_bad_date, old_mtime),
            (u_multi_thr, old_mtime),
            (u_sub_shared, old_mtime),
            (u_sub_old, old_mtime),
        ):
            f = acp / "conversations" / f"{uid}.db"
            f.write_bytes(b"X" * 1024)
            os.utime(f, (mt, mt))

        # Wire u_sub_shared under BOTH u_both_old (old) and u_recent_del (recent); u_sub_old under u_both_old only
        for parent, child in ((u_both_old, u_sub_shared), (u_recent_del, u_sub_shared), (u_both_old, u_sub_old)):
            sdir = acp / "brain" / parent / ".system_generated" / "subagents"
            sdir.mkdir(parents=True, exist_ok=True)
            jfile = sdir / f"{child}.json"
            jfile.write_text(json.dumps({"conversationId": child}))
        for parent in (u_both_old, u_recent_del):
            bdir = acp / "brain" / parent
            for root_d, dirs, files in os.walk(bdir, topdown=False):
                for fn in files:
                    os.utime(os.path.join(root_d, fn), (old_mtime, old_mtime))
                os.utime(root_d, (old_mtime, old_mtime))

        _, reps = mod.build_plan(t, include_superseded=False, include_unknown=False, min_age_hours=24, skip_lsof=True)
        by_id = {s.uuid: s for s in reps[0].sessions}
        self.assertTrue(by_id[u_recent_del].too_recent)
        self.assertFalse(by_id[u_recent_del].selected)
        self.assertTrue(by_id[u_recent_mt].too_recent)
        self.assertFalse(by_id[u_both_old].too_recent)
        self.assertTrue(by_id[u_both_old].selected)
        self.assertTrue(by_id[u_bad_date].too_recent)
        self.assertTrue(by_id[u_multi_thr].too_recent)
        self.assertTrue(by_id[u_sub_shared].too_recent)
        self.assertFalse(by_id[u_sub_old].too_recent)
        self.assertTrue(by_id[u_sub_old].selected)

        # --min-age 0 disables cooldown even when deleted_at is unparseable
        _, reps0 = mod.build_plan(t, include_superseded=False, include_unknown=False, min_age_hours=0, skip_lsof=True)
        by_id0 = {s.uuid: s for s in reps0[0].sessions}
        self.assertFalse(by_id0[u_bad_date].too_recent)
        self.assertTrue(by_id0[u_bad_date].selected)
        self.assertFalse(by_id0[u_recent_del].too_recent)
        self.assertTrue(by_id0[u_recent_del].selected)

    def test_r04_reverify_blocks_mtime_change_cooldown_and_category_change(self):
        t = self.root / "t_r04"
        acps, del_ids = make_fixture(t, del_sizes=(4096, 4096))
        acp = acps[0]
        # Make a 3rd deleted session that is ALREADY in cooldown at preview time
        u_cd = "22222222-2222-2222-2222-000000000099"
        conn = sqlite3.connect(t / "userdata" / "statev2.sqlite")
        conn.execute("INSERT INTO orchestration_v2_projection_threads VALUES (?,?,?,?,?,?)", ("t-cd", "Cooldown", "antigravity", "antigravity", None, iso_utc_ago(1.0)))
        conn.execute("INSERT INTO orchestration_v2_projection_provider_threads VALUES (?,?,?,?)", ("t-cd", "antigravity", f"native-thread:{u_cd}", json.dumps({"nativeThreadRef": {"driver": "antigravity", "nativeId": u_cd}})))
        conn.commit()
        conn.close()
        f_cd = acp / "conversations" / f"{u_cd}.db"
        f_cd.write_bytes(b"C" * 1024)
        cd_mt = time.time() - 3600
        os.utime(f_cd, (cd_mt, cd_mt))

        _, reps = mod.build_plan(t, include_superseded=False, include_unknown=False, min_age_hours=24, skip_lsof=True)
        by_id = {s.uuid: s for s in reps[0].sessions}
        s_mt = by_id[del_ids[0]]
        s_cat = by_id[del_ids[1]]
        s_cd = by_id[u_cd]
        self.assertTrue(s_cd.too_recent)

        # (1) Cooldown session with NO change -> still approved (e.g. Deep Clean / Custom)
        approved0, blocked0 = mod.reverify_before_execute(t, mod.Translator("en"), [s_cd], skip_lsof=True, min_age_hours=24.0)
        self.assertEqual([s.uuid for s in approved0], [u_cd])
        self.assertEqual(blocked0, [])

        # (2) Mutate: update mtime on s_mt AND on s_cd; turn s_cat from DELETED into UNKNOWN
        os.utime(acp / "conversations" / f"{del_ids[0]}.db", (time.time(), time.time()))
        os.utime(f_cd, (time.time(), time.time()))
        conn = sqlite3.connect(t / "userdata" / "statev2.sqlite")
        conn.execute("DELETE FROM orchestration_v2_projection_provider_threads WHERE thread_id = 't-del-1'")
        conn.commit()
        conn.close()

        approved, blocked = mod.reverify_before_execute(
            t, mod.Translator("en"), [s_mt, s_cat, s_cd], skip_lsof=True, min_age_hours=24.0
        )
        self.assertEqual(approved, [])
        blocked_map = {s.uuid: reason for s, reason in blocked}
        self.assertIn(del_ids[0], blocked_map)
        self.assertIn(del_ids[1], blocked_map)
        self.assertIn(u_cd, blocked_map)

    def test_r05_restore_script_uuid_conflict_and_interrupted_resume(self):
        t = self.root / "t_r05"
        acp = t / "acp"
        conv = acp / "conversations"
        conv.mkdir(parents=True)
        u1 = "55555555-5555-5555-5555-000000000001"
        u2 = "55555555-5555-5555-5555-000000000002"
        u3 = "55555555-5555-5555-5555-000000000003"

        bundle = t / "bundle"
        (bundle / "conversations").mkdir(parents=True)
        # u1: both .db and .db-wal in bundle, but NEW .db already exists at target -> skip ENTIRE u1 group!
        (bundle / "conversations" / f"{u1}.db").write_bytes(b"OLD_DB")
        (bundle / "conversations" / f"{u1}.db-wal").write_bytes(b"OLD_WAL")
        (conv / f"{u1}.db").write_bytes(b"NEW_DB")

        # u2: normal restore -> succeeds
        (bundle / "conversations" / f"{u2}.db").write_bytes(b"U2_DB")

        # u3: unlisted .db-wal already exists at target while bundle has .db -> skip ENTIRE u3 group!
        (bundle / "conversations" / f"{u3}.db").write_bytes(b"U3_DB")
        (conv / f"{u3}.db-wal").write_bytes(b"UNLISTED_WAL")

        entries = [
            {"uuid": u1, "bundle_relpath": f"conversations/{u1}.db", "original_path": str((conv / f"{u1}.db").resolve())},
            {"uuid": u1, "bundle_relpath": f"conversations/{u1}.db-wal", "original_path": str((conv / f"{u1}.db-wal").resolve())},
            {"uuid": u2, "bundle_relpath": f"conversations/{u2}.db", "original_path": str((conv / f"{u2}.db").resolve())},
            {"uuid": u3, "bundle_relpath": f"conversations/{u3}.db", "original_path": str((conv / f"{u3}.db").resolve())},
        ]
        rsh = bundle / "restore.sh"
        rsh.write_text(mod.build_restore_script(entries, acp.resolve()))
        rsh.chmod(0o755)
        res = subprocess.run(["bash", str(rsh)], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual((conv / f"{u1}.db").read_bytes(), b"NEW_DB")
        self.assertFalse((conv / f"{u1}.db-wal").exists(), "Old WAL must NOT be placed next to new DB")
        self.assertTrue((conv / f"{u2}.db").exists())
        self.assertFalse((conv / f"{u3}.db").exists(), "Unlisted sidecar at target must block group")

        # Interrupted move / interrupted restore resume: u4 has .db in bundle, .db-wal in manifest & at target (not in bundle) -> NOT a conflict!
        u4 = "55555555-5555-5555-5555-000000000004"
        b2 = t / "bundle2"
        (b2 / "conversations").mkdir(parents=True)
        (b2 / "conversations" / f"{u4}.db").write_bytes(b"U4_DB")
        (conv / f"{u4}.db-wal").write_bytes(b"U4_WAL_STILL_AT_TARGET")
        e2 = [
            {"uuid": u4, "bundle_relpath": f"conversations/{u4}.db", "original_path": str((conv / f"{u4}.db").resolve())},
            {"uuid": u4, "bundle_relpath": f"conversations/{u4}.db-wal", "original_path": str((conv / f"{u4}.db-wal").resolve())},
        ]
        rsh2 = b2 / "restore.sh"
        rsh2.write_text(mod.build_restore_script(e2, acp.resolve()))
        rsh2.chmod(0o755)
        res2 = subprocess.run(["bash", str(rsh2)], capture_output=True, text=True)
        self.assertEqual(res2.returncode, 0, res2.stdout + res2.stderr)
        self.assertTrue((conv / f"{u4}.db").exists())
        self.assertEqual((conv / f"{u4}.db-wal").read_bytes(), b"U4_WAL_STILL_AT_TARGET")

    def test_r06_summaries_restore_merges_rows_and_checks_lsof(self):
        t = self.root / "t_r06"
        orphans = ("33333333-3333-3333-3333-000000000001",)
        acps, _ = make_fixture(t, del_sizes=(), orphan_ids=orphans)
        sdb = acps[0] / "conversation_summaries.db"

        r = subprocess.run([str(SCRIPT), "--t3-home", str(t), "--apply", "-y", "--prune-summaries", "--skip-lsof"], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        backup = next(self.trash_dir.glob("t3-session-cleaner-summaries-backup-*"))
        restore_sh = backup / "restore.sh"

        # Add a NEW row after backup; restore must keep new row AND bring back pruned row
        u_new = "66666666-6666-6666-6666-000000000001"
        conn = sqlite3.connect(sdb)
        conn.execute("INSERT INTO conversation_summaries VALUES (?,?,?,?,?)", (u_new, "Post-backup", 7, "2026-11-25", ""))
        # A NULL id in the live table must not make the restore silently skip every row.
        conn.execute("INSERT INTO conversation_summaries VALUES (?,?,?,?,?)", (None, "Null id", 1, "2026-11-25", ""))
        conn.commit()
        conn.close()

        # (1) lsof missing -> refused
        no_lsof_env = {**self.env, "PATH": "/nonexistent_dir_xyz"}
        r_nolsof = subprocess.run(["/bin/bash", str(restore_sh)], env=no_lsof_env, capture_output=True, text=True)
        self.assertNotEqual(r_nolsof.returncode, 0)

        # (2) lsof returns error (exit 2) -> refused
        bad_lsof_dir = self.root / "bad_lsof_bin"
        bad_lsof_dir.mkdir()
        bad_lsof = bad_lsof_dir / "lsof"
        bad_lsof.write_text("#!/bin/sh\necho 'permission denied' >&2\nexit 2\n")
        bad_lsof.chmod(0o755)
        r_badlsof = subprocess.run(["/bin/bash", str(restore_sh)], env={**self.env, "PATH": f"{bad_lsof_dir}:{os.environ['PATH']}"}, capture_output=True, text=True)
        self.assertNotEqual(r_badlsof.returncode, 0)

        # (2b) T3 still running (statev2.sqlite open) even though nobody holds the index -> refused
        hold = sqlite3.connect(t / "userdata" / "statev2.sqlite")
        hold.execute("SELECT count(*) FROM orchestration_v2_projection_threads").fetchone()
        try:
            r_t3 = subprocess.run(["/bin/bash", str(restore_sh)], env=self.env, capture_output=True, text=True)
        finally:
            hold.close()
        self.assertNotEqual(r_t3.returncode, 0)
        self.assertIn("may still be running", r_t3.stderr)

        # (3) Normal merge restore -> restores orphan row AND keeps u_new
        r_merge = subprocess.run(["/bin/bash", str(restore_sh)], env=self.env, capture_output=True, text=True)
        self.assertEqual(r_merge.returncode, 0, r_merge.stdout + r_merge.stderr)
        self.assertIn("1", r_merge.stdout)
        conn = sqlite3.connect(sdb)
        ids = {row[0] for row in conn.execute("SELECT conversation_id FROM conversation_summaries").fetchall()}
        conn.close()
        self.assertIn(orphans[0], ids)
        self.assertIn(u_new, ids)

        # (4) Constraint violation (e.g. CHECK) must roll back transaction and leave target DB unchanged
        t_chk = self.root / "t_r06_chk"
        trash_chk = self.root / "trash_r06_chk"
        stub_chk = make_stub_trash(self.root / "bin", trash_chk, name="trash_r06_chk")
        u_del_valid = "33333333-3333-3333-3333-000000000099"
        acps_c, _ = make_fixture(t_chk, del_sizes=(), orphan_ids=orphans)
        # Add a deleted thread row (step_count=3) so backup has BOTH a valid row (3) and a CHECK-failing row (0)
        c_t3 = sqlite3.connect(t_chk / "userdata" / "statev2.sqlite")
        c_t3.execute("INSERT INTO orchestration_v2_projection_threads VALUES (?,?,?,?,?,?)", ("t-chk-del", "Chk", "antigravity", "antigravity", None, iso_utc_ago(72)))
        c_t3.execute("INSERT INTO orchestration_v2_projection_provider_threads VALUES (?,?,?,?)", ("t-chk-del", "antigravity", f"native-thread:{u_del_valid}", json.dumps({"nativeThreadRef": {"driver": "antigravity", "nativeId": u_del_valid}})))
        c_t3.commit()
        c_t3.close()
        sdb_c = acps_c[0] / "conversation_summaries.db"
        cc0 = sqlite3.connect(sdb_c)
        cc0.execute("INSERT INTO conversation_summaries VALUES (?,?,?,?,?)", (u_del_valid, "Valid", 3, "2026-11-20", ""))
        cc0.commit()
        cc0.close()

        r_c = subprocess.run(
            [str(SCRIPT), "--t3-home", str(t_chk), "--apply", "-y", "--prune-summaries", "--skip-lsof"],
            env={**self.env, "T3_CLEAN_TRASH_BIN": str(stub_chk)},
            capture_output=True, text=True,
        )
        self.assertEqual(r_c.returncode, 0, r_c.stdout + r_c.stderr)
        b_c = next(trash_chk.glob("t3-session-cleaner-summaries-backup-*"))
        # Recreate target table with strict CHECK(step_count > 0) so restoring 0-step orphan fails transaction
        sdb_c.unlink()
        for suf in ("-wal", "-shm", "-journal"):
            p = Path(str(sdb_c) + suf)
            if p.exists():
                p.unlink()
        cc = sqlite3.connect(sdb_c)
        cc.execute(
            "CREATE TABLE conversation_summaries "
            "(conversation_id TEXT PRIMARY KEY, title TEXT, step_count INT CHECK(step_count > 0), last_modified_time TEXT, parent_conversation_id TEXT)"
        )
        cc.execute("INSERT INTO conversation_summaries VALUES (?,?,?,?,?)", ("11111111-1111-1111-1111-111111111111", "Active", 5, "2026-11-25", ""))
        cc.commit()
        cc.close()
        r_chk_fail = subprocess.run(["/bin/bash", str(b_c / "restore.sh")], env=self.env, capture_output=True, text=True)
        self.assertNotEqual(r_chk_fail.returncode, 0)
        cc = sqlite3.connect(sdb_c)
        self.assertEqual(cc.execute("SELECT COUNT(*) FROM conversation_summaries").fetchone()[0], 1)
        cc.close()

        # (5) Schema mismatch -> refused without altering target
        conn = sqlite3.connect(sdb)
        conn.execute("ALTER TABLE conversation_summaries ADD COLUMN extra_col TEXT")
        conn.commit()
        conn.close()
        r_mismatch = subprocess.run(["/bin/bash", str(restore_sh)], env=self.env, capture_output=True, text=True)
        self.assertNotEqual(r_mismatch.returncode, 0)

        # (6) Target DB missing -> refused, never recreated
        for suf in ("", "-wal", "-shm", "-journal"):
            p = Path(str(sdb) + suf)
            if p.exists():
                p.unlink()
        r_missing = subprocess.run(["/bin/bash", str(restore_sh)], env=self.env, capture_output=True, text=True)
        self.assertNotEqual(r_missing.returncode, 0)
        self.assertFalse(sdb.exists())

    def test_r07_statev2_lsof_blocks_index_prune_even_without_agy(self):
        t = self.root / "t_r07"
        orphans = ("33333333-3333-3333-3333-000000000001",)
        acps, _ = make_fixture(t, del_sizes=(), orphan_ids=orphans)
        state_db = t / "userdata" / "statev2.sqlite"

        # Spawn helper holding ONLY statev2.sqlite open (no agy, no profile path)
        holder = subprocess.Popen(
            [sys.executable, "-c", f"import sys, time; f = open({str(state_db)!r}, 'rb'); print('READY', flush=True); time.sleep(30)"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            self.assertEqual(holder.stdout.readline().strip(), "READY")
            _, reps = mod.build_plan(t, include_superseded=False, include_unknown=False, min_age_hours=24, skip_lsof=False)
            snap = mod.build_menu_snapshot(reps)
            self.assertTrue(snap.profiles_busy, "Open statev2.sqlite must mark T3 as busy")

            r_skip = subprocess.run([str(SCRIPT), "--t3-home", str(t), "--apply", "-y", "--prune-summaries"], env=self.env, capture_output=True, text=True)
            self.assertEqual(r_skip.returncode, 0)
            c = sqlite3.connect(acps[0] / "conversation_summaries.db")
            self.assertEqual(c.execute("SELECT COUNT(*) FROM conversation_summaries").fetchone()[0], 2)
            c.close()
        finally:
            holder.terminate()
            holder.wait(timeout=3)
            holder.stdout.close()
            holder.stderr.close()

        # Failing lsof must also refuse index prune
        bad_lsof_dir = self.root / "r07_bad_lsof"
        bad_lsof_dir.mkdir()
        bad_lsof = bad_lsof_dir / "lsof"
        bad_lsof.write_text("#!/bin/sh\necho 'lsof error' >&2\nexit 2\n")
        bad_lsof.chmod(0o755)
        r_bad = subprocess.run(
            [str(SCRIPT), "--t3-home", str(t), "--apply", "-y", "--prune-summaries"],
            env={**self.env, "PATH": f"{bad_lsof_dir}:{os.environ['PATH']}"},
            capture_output=True, text=True,
        )
        self.assertEqual(r_bad.returncode, 2)
        c = sqlite3.connect(acps[0] / "conversation_summaries.db")
        self.assertEqual(c.execute("SELECT COUNT(*) FROM conversation_summaries").fetchone()[0], 2)
        c.close()

        # Once holder exits, tool's own read-only connections must NOT trigger false positive
        r_ok = subprocess.run([str(SCRIPT), "--t3-home", str(t), "--apply", "-y", "--prune-summaries"], env=self.env, capture_output=True, text=True)
        self.assertEqual(r_ok.returncode, 0, r_ok.stdout + r_ok.stderr)
        c = sqlite3.connect(acps[0] / "conversation_summaries.db")
        self.assertEqual(c.execute("SELECT COUNT(*) FROM conversation_summaries").fetchone()[0], 1)
        c.close()

    def test_r08_process_check_does_not_ignore_t3_session_cleaner_in_other_pid(self):
        prof = self.root / "fake_profile_hash"
        prof.mkdir()
        self.assertFalse(mod.is_profile_in_use_by_process(prof))
        holder = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)", str(prof.resolve()), "/tmp/t3-session-cleaner"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(0.15)
            self.assertTrue(mod.is_profile_in_use_by_process(prof))
        finally:
            holder.terminate()
            holder.wait(timeout=3)

    def test_r09_yes_flag_does_not_bypass_tui_confirmation(self):
        t = self.root / "t_r09"
        acps, del_ids = make_fixture(t, del_sizes=(4096,))
        target = acps[0] / "conversations" / f"{del_ids[0]}.db"

        # -y must STILL show the TUI confirm view; cancelling there leaves everything untouched.
        rc, caps = run_pty(
            [str(SCRIPT), "--t3-home", str(t), "-i", "-y", "--skip-lsof", "--lang", "en"],
            self.env,
            cols=92,
            rows=26,
            steps=[
                (b"\r", "Confirm Cleanup"),  # Enter on Standard clean -> Confirm screen
                (b"n", "Cancelled."),
                b"q",
            ],
        )
        self.assertEqual(rc, 0)
        self.assertIn("Confirm Cleanup", strip_ansi(caps[1]))
        self.assertIn("Cancelled.", strip_ansi(caps[2]))
        self.assertTrue(target.exists(), "-y must not bypass TUI confirmation")

    def test_r10_staging_manifest_and_restore_written_before_move_and_warning_has_path(self):
        t = self.root / "t_r10"
        acps, del_ids = make_fixture(t, del_sizes=(4096,))
        acp = acps[0]
        f1 = acp / "conversations" / f"{del_ids[0]}.db"
        f2 = acp / "conversations" / f"{del_ids[0]}.db-wal"
        f2.write_bytes(b"WAL_PART")
        old_ts = time.time() - 72 * 3600
        os.utime(f2, (old_ts, old_ts))

        _, reps = mod.build_plan(t, include_superseded=False, include_unknown=False, min_age_hours=24, skip_lsof=True)
        chosen = [s for s in reps[0].sessions if s.selected]

        real_rename = os.rename
        calls = {"n": 0}

        def interrupt_on_second(src, dst):
            calls["n"] += 1
            if calls["n"] == 2:
                raise KeyboardInterrupt("simulated Ctrl-C mid-move")
            return real_rename(src, dst)

        with mock.patch.dict(os.environ, {"T3_CLEAN_TRASH_BIN": str(self.trash_stub)}):
            with mock.patch("os.rename", side_effect=interrupt_on_second):
                with self.assertRaises(KeyboardInterrupt):
                    mod.trash_profile_bundle(acp, chosen, mod.Translator("en"), ts="2026-11-25_000000", multi_profile=False)

        staging = acp.parent / ".t3-clean-staging"
        bundles = list(staging.glob("t3-session-cleaner-*"))
        self.assertEqual(len(bundles), 1)
        bundle_with = bundles[0]
        self.assertTrue((bundle_with / "manifest.json").is_file())
        self.assertTrue((bundle_with / "restore.sh").is_file())

        # Mixed staging dir: one bundle WITH restore.sh + one bundle WITHOUT restore.sh
        bundle_without = staging / "manual-leftover-bundle"
        bundle_without.mkdir()
        (bundle_without / "orphan.db").write_bytes(b"KEEP")

        r_warn = subprocess.run([str(SCRIPT), "--t3-home", str(t), "-n", "--skip-lsof"], env=self.env, capture_output=True, text=True)
        lines_warn = r_warn.stdout.splitlines()
        with_lines = [l for l in lines_warn if str(bundle_with) in l]
        without_lines = [l for l in lines_warn if str(bundle_without) in l]
        self.assertEqual(len(with_lines), 1, f"Expected dedicated warning for bundle_with:\n{r_warn.stdout}")
        self.assertIn("restore.sh", with_lines[0])
        self.assertEqual(len(without_lines), 1, f"Expected dedicated warning for bundle_without:\n{r_warn.stdout}")
        self.assertNotIn("restore.sh", without_lines[0])
        self.assertIn("do not delete", without_lines[0].lower())

        # Running restore.sh recovers the moved file cleanly
        r_rest = subprocess.run(["bash", str(bundle_with / "restore.sh")], capture_output=True, text=True)
        self.assertEqual(r_rest.returncode, 0, r_rest.stdout + r_rest.stderr)
        self.assertTrue(f1.exists() and f2.exists())

        # When restore.sh skips a conflicting session group, exit 0 AND warn not to delete bundle dir
        conflict_bundle = self.root / "r10_conflict_bundle"
        (conflict_bundle / "conversations").mkdir(parents=True)
        (conflict_bundle / "conversations" / f"{del_ids[0]}.db").write_bytes(b"OLD_COPY")
        crsh = conflict_bundle / "restore.sh"
        crsh.write_text(mod.build_restore_script([
            {"uuid": del_ids[0], "bundle_relpath": f"conversations/{del_ids[0]}.db", "original_path": str(f1.resolve())},
        ], acps[0].resolve()))
        crsh.chmod(0o755)
        r_skip = subprocess.run(["bash", str(crsh)], capture_output=True, text=True)
        self.assertEqual(r_skip.returncode, 0, r_skip.stdout + r_skip.stderr)
        self.assertIn("do not delete", r_skip.stdout.lower())
        m_bundle = re.search(r"remain only in (.+?) — do not delete", r_skip.stdout)
        self.assertIsNotNone(m_bundle, f"Expected bundle path in warning:\n{r_skip.stdout}")
        self.assertEqual(Path(m_bundle.group(1)).resolve(), conflict_bundle.resolve())

    def test_r11_execute_cleanup_catches_contract_error_returns_2(self):
        # Case 1: statev3 appears before reverify -> returns 2, no traceback, files untouched
        t1 = self.root / "t_r11a"
        acps1, del1 = make_fixture(t1, del_sizes=(4096,))
        _, reps1 = mod.build_plan(t1, include_superseded=False, include_unknown=False, min_age_hours=24, skip_lsof=True)
        chosen1 = [s for s in reps1[0].sessions if s.selected]
        (t1 / "userdata" / "statev3.sqlite").write_bytes(b"")
        err_buf, out_buf = io.StringIO(), io.StringIO()
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            rc1 = mod.execute_cleanup(t1, reps1, chosen1, prune_summaries=False, skip_lsof=True, st=mod.Style(False), tr=mod.Translator("en"))
        self.assertEqual(rc1, 2)
        self.assertNotIn("Traceback", err_buf.getvalue())
        self.assertTrue((acps1[0] / "conversations" / f"{del1[0]}.db").exists())

        # Case 2: files already trashed, then statev3 appears before index prune -> reports bundle & freed bytes AND returns 2
        t2 = self.root / "t_r11b"
        acps2, del2 = make_fixture(t2, del_sizes=(4096,), orphan_ids=("33333333-3333-3333-3333-000000000001",))
        _, reps2 = mod.build_plan(t2, include_superseded=False, include_unknown=False, min_age_hours=24, skip_lsof=True)
        chosen2 = [s for s in reps2[0].sessions if s.selected]

        stub_drop_v3 = self.root / "bin" / "trash_drop_v3"
        stub_drop_v3.write_text(
            "#!/usr/bin/env python3\n"
            "import shutil, sys\n"
            "from pathlib import Path\n"
            f"dst_root = Path({str(self.trash_dir)!r})\n"
            "for a in sys.argv[1:]:\n"
            "    if a.startswith('-'): continue\n"
            "    shutil.move(a, str(dst_root / Path(a).name))\n"
            f"Path({str(t2 / 'userdata' / 'statev3.sqlite')!r}).write_bytes(b'')\n"
        )
        stub_drop_v3.chmod(0o755)
        err2, out2 = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"T3_CLEAN_TRASH_BIN": str(stub_drop_v3)}):
            with redirect_stdout(out2), redirect_stderr(err2):
                rc2 = mod.execute_cleanup(t2, reps2, chosen2, prune_summaries=True, skip_lsof=True, st=mod.Style(False), tr=mod.Translator("en"))
        self.assertEqual(rc2, 2)
        self.assertIn("Moved bundle to Trash", out2.getvalue())
        self.assertIn("4.0 KB", out2.getvalue())

        # Case 3: index-only prune (no files chosen) still trips the circuit breaker if T3 data drifts after preview
        t3 = self.root / "t_r11c"
        orphan = "33333333-3333-3333-3333-000000000001"
        acps3, _ = make_fixture(t3, del_sizes=(), orphan_ids=(orphan,))
        _, reps3 = mod.build_plan(t3, include_superseded=False, include_unknown=False, min_age_hours=24, skip_lsof=True)
        drift = "99999999-9999-9999-9999-999999999999"
        c3 = sqlite3.connect(t3 / "userdata" / "statev2.sqlite")
        c3.execute(
            "UPDATE orchestration_v2_projection_provider_threads SET provider_thread_id=?, payload_json=? WHERE thread_id='t-act'",
            (f"native-thread:{drift}", json.dumps({"nativeThreadRef": {"driver": "antigravity", "nativeId": drift}})),
        )
        c3.commit()
        c3.close()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err3:
            rc3 = mod.execute_cleanup(t3, reps3, [], prune_summaries=True, skip_lsof=True, st=mod.Style(False), tr=mod.Translator("en"))
        self.assertEqual(rc3, 2)
        self.assertIn("circuit breaker", err3.getvalue())
        c3 = sqlite3.connect(acps3[0] / "conversation_summaries.db")
        self.assertEqual(c3.execute("SELECT count(*) FROM conversation_summaries WHERE conversation_id=?", (orphan,)).fetchone()[0], 1)
        c3.close()

    def test_r12_multiprofile_partial_failure_reports_separately_and_skips_index(self):
        t = self.root / "t_r12"
        orphans = ("33333333-3333-3333-3333-000000000001",)
        acps, del_ids = make_fixture(t, del_sizes=(4096,), orphan_ids=orphans, multi_profile=True)
        # Put a deleted session on profile 2 as well
        f_p2 = acps[1] / "conversations" / f"{del_ids[0]}.db"
        f_p2.write_bytes(b"P" * 2048)
        old_ts = time.time() - 72 * 3600
        os.utime(f_p2, (old_ts, old_ts))

        # Stub trash succeeds for aaaa1111, fails for cccc3333
        stub_partial = self.root / "bin" / "trash_partial"
        stub_partial.write_text(
            "#!/usr/bin/env python3\n"
            "import shutil, sys\n"
            "from pathlib import Path\n"
            f"dst_root = Path({str(self.trash_dir)!r})\n"
            "for a in sys.argv[1:]:\n"
            "    if a.startswith('-'): continue\n"
            "    if 'cccc3333' in a:\n"
            "        sys.stderr.write('disk full on profile 2\\n')\n"
            "        sys.exit(1)\n"
            "    shutil.move(a, str(dst_root / Path(a).name))\n"
        )
        stub_partial.chmod(0o755)
        r = subprocess.run(
            [str(SCRIPT), "--t3-home", str(t), "--apply", "-y", "--prune-summaries", "--skip-lsof"],
            env={**self.env, "T3_CLEAN_TRASH_BIN": str(stub_partial)},
            capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 1)
        self.assertIn("aaaa1111", r.stdout)
        self.assertIn("cccc3333", r.stderr)
        self.assertIn("moved 1 session(s) to Trash, freed 4.0 KB.", r.stdout)
        self.assertNotIn("moved 2 session(s)", r.stdout)
        # Neither profile's summaries.db should have been pruned when any profile failed
        for acp in acps:
            c = sqlite3.connect(acp / "conversation_summaries.db")
            ids = {row[0] for row in c.execute("SELECT conversation_id FROM conversation_summaries").fetchall()}
            c.close()
            self.assertIn(orphans[0], ids)

    def test_r13_noninteractive_apply_without_yes_exits_1(self):
        t = self.root / "t_r13"
        make_fixture(t, del_sizes=(4096,))
        # Non-TTY --apply without -y -> exit 1
        r = subprocess.run([str(SCRIPT), "--t3-home", str(t), "--apply", "--skip-lsof"], env=self.env, stdin=subprocess.DEVNULL, capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)

        # Interactive TTY decline -> exit 0
        rc_tty, _ = run_pty(
            [str(SCRIPT), "--t3-home", str(t), "--apply", "--skip-lsof"],
            self.env,
            steps=[b"n\r"],
        )
        self.assertEqual(rc_tty, 0)

    def test_r14_delete_failure_reports_backup_name(self):
        orphans = ("33333333-3333-3333-3333-000000000001",)
        real_connect = sqlite3.connect

        # DELETE fails after backup -> still reports backup name and returns 1
        t2 = self.root / "t_r14b"
        make_fixture(t2, del_sizes=(), orphan_ids=orphans)
        _, reps2 = mod.build_plan(t2, include_superseded=False, include_unknown=False, min_age_hours=24, skip_lsof=True)

        class DeleteFailConn:
            def __init__(self, inner):
                self._inner = inner

            def cursor(self):
                inner_cur = self._inner.cursor()

                class Cur:
                    def __getattr__(self, k):
                        return getattr(inner_cur, k)

                    def execute(self, sql, *a, **kw):
                        if sql.strip().upper().startswith("DELETE"):
                            raise sqlite3.OperationalError("simulated delete lock")
                        return inner_cur.execute(sql, *a, **kw)

                return Cur()

            def __getattr__(self, k):
                return getattr(self._inner, k)

        def connect_del_fail(path, *a, **kw):
            c = real_connect(path, *a, **kw)
            if not kw.get("uri") and str(path).endswith("conversation_summaries.db") and ".t3-clean-staging" not in str(path):
                return DeleteFailConn(c)
            return c

        out2, err2 = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"T3_CLEAN_TRASH_BIN": str(self.trash_stub)}):
            with mock.patch("sqlite3.connect", side_effect=connect_del_fail):
                with redirect_stdout(out2), redirect_stderr(err2):
                    rc2 = mod.execute_cleanup(t2, reps2, [], prune_summaries=True, skip_lsof=True, st=mod.Style(False), tr=mod.Translator("en"))
        self.assertEqual(rc2, 1)
        self.assertIn("t3-session-cleaner-summaries-backup-", out2.getvalue() + err2.getvalue())

    def test_r15_min_age_rejects_nan_inf_and_negative(self):
        t = self.root / "t_r15"
        acps, del_ids = make_fixture(t, del_sizes=(4096,), del_age_hours=1.0)
        target = acps[0] / "conversations" / f"{del_ids[0]}.db"
        for bad in ("nan", "inf", "-inf", "-1"):
            r = subprocess.run([str(SCRIPT), "--t3-home", str(t), "--min-age", bad, "--apply", "-y", "--skip-lsof"], env=self.env, capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0, f"--min-age {bad} must fail")
            self.assertTrue(target.exists())
        for good in ("0", "0.5"):
            r = subprocess.run([str(SCRIPT), "--t3-home", str(t), "--min-age", good, "-n", "--skip-lsof"], env=self.env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"--min-age {good} must succeed")

    def test_r16_restore_mkdir_failure_exits_nonzero(self):
        t = self.root / "t_r16"
        bundle = t / "bundle"
        u = "77777777-7777-7777-7777-000000000001"
        (bundle / "conversations").mkdir(parents=True)
        src = bundle / "conversations" / f"{u}.db"
        src.write_bytes(b"KEEP_ME")
        # Make target parent a regular file so mkdir -p fails
        blocker = t / "not_a_dir"
        blocker.write_text("file")
        dst = blocker / "conversations" / f"{u}.db"

        rsh = bundle / "restore.sh"
        rsh.write_text(mod.build_restore_script([{"uuid": u, "bundle_relpath": f"conversations/{u}.db", "original_path": str(dst)}], blocker))
        rsh.chmod(0o755)
        res = subprocess.run(["bash", str(rsh)], capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertTrue(src.exists())

    def test_r18_title_strips_terminal_control_chars(self):
        t = self.root / "t_r18"
        acps, del_ids = make_fixture(t, del_sizes=(4096,))
        evil_title = "\x1b[2J\x1b[H\x07\x7f\x9b2J测试🚀"
        conn = sqlite3.connect(t / "userdata" / "statev2.sqlite")
        conn.execute("UPDATE orchestration_v2_projection_threads SET title = ? WHERE thread_id = 't-del-0'", (evil_title,))
        conn.commit()
        conn.close()

        r = subprocess.run([str(SCRIPT), "--t3-home", str(t), "-n", "--no-color", "--skip-lsof"], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        for ctrl in ("\x1b", "\x07", "\x7f", "\x9b"):
            self.assertNotIn(ctrl, r.stdout)
        self.assertIn("测试🚀", r.stdout)

        # Confirm page in TUI also strips control chars from title
        rc_tui, caps_tui = run_pty(
            [str(SCRIPT), "--t3-home", str(t), "--no-color", "--skip-lsof", "--lang", "en"],
            self.env, cols=92, rows=26,
            steps=[b"\r", b"\x1b", b"q"],
        )
        self.assertEqual(rc_tui, 0)
        conf_frame = strip_ansi(caps_tui[1].split("\x1b[H")[-1])
        for ctrl in ("\x07", "\x7f", "\x9b"):
            self.assertNotIn(ctrl, conf_frame)
        self.assertNotIn("[2J", conf_frame)
        self.assertIn("测试🚀", conf_frame)

    def test_q01_narrowed_index_prune_rules_and_staging_skip(self):
        t = self.root / "t_q01"
        acps, _ = make_fixture(t, del_sizes=(), multi_profile=True)
        acp = acps[0]

        u_del = "88888888-8888-8888-8888-000000000001"       # deleted in T3, no disk -> prune
        u_del_sub = "88888888-8888-8888-8888-000000000002"   # subagent of u_del, no disk -> prune
        u_sup = "88888888-8888-8888-8888-000000000003"       # superseded in T3, no disk -> prune
        u_shell = "88888888-8888-8888-8888-000000000004"     # 0-step empty title, no T3 evidence -> prune
        u_keep_steps = "88888888-8888-8888-8888-000000000005"  # steps=5, empty title, no T3 -> KEEP
        u_keep_title = "88888888-8888-8888-8888-000000000006"  # steps=0, has title, no T3 -> KEEP
        u_unk_disk = "88888888-8888-8888-8888-000000000007"    # UNKNOWN on disk (steps=4, title="Unk") -> prunable ONLY if cleaned this run

        conn = sqlite3.connect(t / "userdata" / "statev2.sqlite")
        conn.execute("INSERT INTO orchestration_v2_projection_threads VALUES (?,?,?,?,?,?)", ("t-q-del", "Q Del", "antigravity", "antigravity", None, iso_utc_ago(72)))
        conn.execute("INSERT INTO orchestration_v2_projection_provider_threads VALUES (?,?,?,?)", ("t-q-del", "antigravity", f"native-thread:{u_del}", json.dumps({"nativeThreadRef": {"driver": "antigravity", "nativeId": u_del}})))
        conn.execute("INSERT INTO orchestration_v2_projection_run_attempts VALUES (?,?,?)", ("t-act", "antigravity", json.dumps({"nativeThreadId": u_sup})))
        conn.commit()
        conn.close()

        f_unk = acp / "conversations" / f"{u_unk_disk}.db"
        f_unk.write_bytes(b"U" * 1024)
        old_ts = time.time() - 72 * 3600
        os.utime(f_unk, (old_ts, old_ts))

        sconn = sqlite3.connect(acp / "conversation_summaries.db")
        for cid, title, steps, parent in (
            (u_del, "Deleted", 10, ""),
            (u_del_sub, "Del Sub", 3, u_del),
            (u_sup, "Superseded", 8, ""),
            (u_shell, "", 0, ""),
            (u_keep_steps, "", 5, ""),
            (u_keep_title, "Important", 0, ""),
            (u_unk_disk, "Unk", 4, ""),
        ):
            sconn.execute("INSERT INTO conversation_summaries VALUES (?,?,?,?,?)", (cid, title, steps, "2026-11-20", parent))
        sconn.commit()
        sconn.close()

        # Profile 1 has a staging leftover + 1 empty shell row -> must be skipped during prune with reason
        st_p2 = acps[1].parent / ".t3-clean-staging" / "leftover-bundle"
        st_p2.mkdir(parents=True)
        (st_p2 / "marker").write_text("x")
        sconn2 = sqlite3.connect(acps[1] / "conversation_summaries.db")
        sconn2.execute("INSERT INTO conversation_summaries VALUES (?,?,?,?,?)", (u_shell, "", 0, "", ""))
        sconn2.commit()
        sconn2.close()

        _, reps = mod.build_plan(t, include_superseded=False, include_unknown=True, min_age_hours=24, skip_lsof=True)
        self.assertEqual(set(reps[0].orphan_summary_ids), {u_del, u_del_sub, u_sup, u_shell})
        chosen = [s for s in reps[0].sessions if s.selected]
        self.assertEqual([s.uuid for s in chosen], [u_unk_disk])
        # Estimated count excludes profile 1 (has staging leftover) and includes u_unk_disk (cleaned this run)
        self.assertEqual(mod.estimate_pruned_summaries(reps, chosen), 5)

        r = subprocess.run(
            [str(SCRIPT), "--t3-home", str(t), "--apply", "-y", "--include-unknown", "--prune-summaries", "--skip-lsof"],
            env=self.env, capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(".t3-clean-staging", r.stdout)

        sconn = sqlite3.connect(acp / "conversation_summaries.db")
        rem0 = {row[0] for row in sconn.execute("SELECT conversation_id FROM conversation_summaries").fetchall()}
        sconn.close()
        self.assertEqual(rem0, {"11111111-1111-1111-1111-111111111111", u_keep_steps, u_keep_title})

        # Profile 1's shell row must remain untouched because of staging leftover
        sconn2 = sqlite3.connect(acps[1] / "conversation_summaries.db")
        rem1 = {row[0] for row in sconn2.execute("SELECT conversation_id FROM conversation_summaries").fetchall()}
        sconn2.close()
        self.assertIn(u_shell, rem1)

        # Missing step_count or title column must NEVER be treated as empty shell
        t_nocol = self.root / "t_q01_nocol"
        acps_nc, _ = make_fixture(t_nocol, del_sizes=())
        sdb_nc = acps_nc[0] / "conversation_summaries.db"
        sdb_nc.unlink()
        c_nc = sqlite3.connect(sdb_nc)
        c_nc.execute("CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY)")
        c_nc.execute("INSERT INTO conversation_summaries VALUES (?)", (u_shell,))
        c_nc.commit()
        c_nc.close()
        _, reps_nc = mod.build_plan(t_nocol, include_superseded=False, include_unknown=False, min_age_hours=24, skip_lsof=True)
        self.assertEqual(reps_nc[0].orphan_summary_ids, [])

    def test_r19_rollback_skips_conflicting_uuid_group_preserving_wal(self):
        # Real SQLite WAL test: u1 has .db + .db-wal (old_tbl), u2 has .db.
        # During trash failure, u1.db is recreated with new_tbl.
        # Rollback must NOT put old u1.db-wal next to new u1.db; u1 stays in staging, u2 rolls back cleanly.
        t = self.root / "t_r19"
        acps, del_ids = make_fixture(t, del_sizes=(4096, 2048))
        acp = acps[0]
        conv = acp / "conversations"
        u1, u2 = del_ids[0], del_ids[1]
        u1_db = conv / f"{u1}.db"
        u1_wal = conv / f"{u1}.db-wal"
        u2_db = conv / f"{u2}.db"

        # Create a genuine uncheckpointed SQLite WAL file reliably across Python 3.9 and 3.12
        u1_db.unlink()
        wal_src = self.root / "r19_wal_src.db"
        c_old = sqlite3.connect(wal_src)
        c_old.execute("PRAGMA journal_mode=WAL")
        c_old.execute("PRAGMA wal_autocheckpoint=0")
        c_old.execute("CREATE TABLE old_tbl (x INT)")
        c_old.execute("INSERT INTO old_tbl VALUES (42)")
        c_old.commit()
        u1_db.write_bytes(wal_src.read_bytes())
        u1_wal.write_bytes(Path(str(wal_src) + "-wal").read_bytes())
        c_old.close()
        old_ts = time.time() - 72 * 3600
        for p in conv.iterdir():
            os.utime(p, (old_ts, old_ts))

        stub_recreate = self.root / "bin" / "trash_r19"
        stub_recreate.write_text(
            "#!/usr/bin/env python3\n"
            "import sqlite3, sys\n"
            f"c = sqlite3.connect({str(u1_db)!r})\n"
            "c.execute('CREATE TABLE new_tbl (y INT)')\n"
            "c.execute('INSERT INTO new_tbl VALUES (99)')\n"
            "c.commit()\n"
            "c.close()\n"
            "sys.stderr.write('simulated trash fail\\n')\n"
            "sys.exit(1)\n"
        )
        stub_recreate.chmod(0o755)
        r = subprocess.run(
            [str(SCRIPT), "--t3-home", str(t), "--apply", "-y", "--skip-lsof"],
            env={**self.env, "T3_CLEAN_TRASH_BIN": str(stub_recreate)},
            capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 1)
        self.assertFalse(u1_wal.exists(), "Old WAL must NOT be rolled back next to recreated u1.db")
        c_new = sqlite3.connect(u1_db)
        tables = {row[0] for row in c_new.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        c_new.close()
        self.assertEqual(tables, {"new_tbl"})
        self.assertTrue(u2_db.exists(), "Non-conflicting u2 must still roll back normally")

        staging = acp.parent / ".t3-clean-staging"
        bundles = list(staging.glob("t3-session-cleaner-*"))
        self.assertEqual(len(bundles), 1)
        self.assertTrue((bundles[0] / "conversations" / f"{u1}.db").is_file())
        self.assertTrue((bundles[0] / "conversations" / f"{u1}.db-wal").is_file())
        self.assertIn(str(bundles[0]), r.stdout + r.stderr)

        # Also test unlisted sidecar conflict during rollback: u_only had only .db moved,
        # during trash failure an unlisted .db-wal appears at target -> u_only.db must stay in staging
        t2 = self.root / "t_r19b"
        acps2, del2 = make_fixture(t2, del_sizes=(4096,))
        u_only = del2[0]
        unlisted_wal = acps2[0] / "conversations" / f"{u_only}.db-wal"
        stub_unlisted = self.root / "bin" / "trash_r19_unlisted"
        stub_unlisted.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "from pathlib import Path\n"
            f"Path({str(unlisted_wal)!r}).write_bytes(b'UNLISTED_WAL')\n"
            "sys.exit(1)\n"
        )
        stub_unlisted.chmod(0o755)
        r2 = subprocess.run(
            [str(SCRIPT), "--t3-home", str(t2), "--apply", "-y", "--skip-lsof"],
            env={**self.env, "T3_CLEAN_TRASH_BIN": str(stub_unlisted)},
            capture_output=True, text=True,
        )
        self.assertEqual(r2.returncode, 1)
        self.assertFalse((acps2[0] / "conversations" / f"{u_only}.db").exists())
        bundles2 = list((acps2[0].parent / ".t3-clean-staging").glob("t3-session-cleaner-*"))
        self.assertEqual(len(bundles2), 1)
        self.assertTrue((bundles2[0] / "conversations" / f"{u_only}.db").is_file())

    def test_r20_rollback_reports_uncertain_when_trash_moved_bundle_but_exited_nonzero(self):
        t = self.root / "t_r20"
        orphans = ("33333333-3333-3333-3333-000000000001",)
        acps, _ = make_fixture(t, del_sizes=(4096,), orphan_ids=orphans)

        stub_move_and_fail = self.root / "bin" / "trash_move_fail"
        stub_move_and_fail.write_text(
            "#!/usr/bin/env python3\n"
            "import shutil, sys\n"
            "from pathlib import Path\n"
            f"dst_root = Path({str(self.trash_dir)!r})\n"
            "for a in sys.argv[1:]:\n"
            "    if a.startswith('-'): continue\n"
            "    p = Path(a)\n"
            "    dst = dst_root / p.name\n"
            "    i = 1\n"
            "    while dst.exists():\n"
            "        dst = dst_root / f'{p.name}-{i}'\n"
            "        i += 1\n"
            "    shutil.move(str(p), str(dst))\n"
            "sys.stderr.write('trash exited 1 after move\\n')\n"
            "sys.exit(1)\n"
        )
        stub_move_and_fail.chmod(0o755)
        r = subprocess.run(
            [str(SCRIPT), "--t3-home", str(t), "--apply", "-y", "--prune-summaries", "--skip-lsof"],
            env={**self.env, "T3_CLEAN_TRASH_BIN": str(stub_move_and_fail)},
            capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 1)
        combined = r.stdout + r.stderr
        self.assertNotIn("rolled back", combined.lower())
        self.assertIn("trash", combined.lower())
        trashed = list(self.trash_dir.glob("t3-session-cleaner-*"))
        self.assertEqual(len(trashed), 1)
        self.assertIn(trashed[0].name, combined)
        # Must NOT prune summaries when trash status is uncertain
        c = sqlite3.connect(acps[0] / "conversation_summaries.db")
        ids = {row[0] for row in c.execute("SELECT conversation_id FROM conversation_summaries").fetchall()}
        c.close()
        self.assertIn(orphans[0], ids)

    def test_r21_pre_move_write_error_cleans_up_empty_staging_dir(self):
        t = self.root / "t_r21"
        acps, del_ids = make_fixture(t, del_sizes=(4096,))
        acp = acps[0]
        target = acp / "conversations" / f"{del_ids[0]}.db"
        _, reps = mod.build_plan(t, include_superseded=False, include_unknown=False, min_age_hours=24, skip_lsof=True)
        chosen = [s for s in reps[0].sessions if s.selected]

        real_write_text = Path.write_text

        def fail_manifest(self_p, *a, **kw):
            if self_p.name == "manifest.json":
                raise OSError("No space left on device")
            return real_write_text(self_p, *a, **kw)

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"T3_CLEAN_TRASH_BIN": str(self.trash_stub)}):
            with mock.patch.object(Path, "write_text", fail_manifest):
                with redirect_stdout(out), redirect_stderr(err):
                    rc = mod.execute_cleanup(t, reps, chosen, prune_summaries=False, skip_lsof=True, st=mod.Style(False), tr=mod.Translator("en"))
        self.assertEqual(rc, 1)
        self.assertTrue(target.exists())
        self.assertFalse((acp.parent / ".t3-clean-staging").exists(), "Empty staging dir must be removed on pre-move failure")

    def test_r22_mutation_guards_and_no_unused_trash_sessions_bundled(self):
        # 0. Dead helper removed
        self.assertFalse(hasattr(mod, "trash_sessions_bundled"))

        # 1. R02 in real TTY: -i -n must fail specifically with argument conflict (not TTY error)
        t_r02 = self.root / "t_r22_r02"
        make_fixture(t_r02, del_sizes=(4096,))
        rc_tty, caps_tty = run_pty([str(SCRIPT), "--t3-home", str(t_r02), "-i", "-n", "--skip-lsof"], self.env, steps=())
        self.assertEqual(rc_tty, 2)
        tty_out = strip_ansi("".join(caps_tty))
        self.assertIn("--dry-run", tty_out)
        self.assertNotIn("requires a TTY", tty_out)

        # 2. R03 multi-parent subagent where OLD parent is inserted BEFORE NEW parent
        t_r03 = self.root / "t_r22_r03"
        acps3, _ = make_fixture(t_r03, del_sizes=())
        acp3 = acps3[0]
        u_old_p = "99999999-9999-9999-9999-000000000001"
        u_new_p = "99999999-9999-9999-9999-000000000002"
        u_child = "99999999-9999-9999-9999-000000000003"
        c3 = sqlite3.connect(t_r03 / "userdata" / "statev2.sqlite")
        # Insert OLD parent FIRST, NEW parent SECOND so FIFO without relaxation would pick OLD!
        c3.execute("INSERT INTO orchestration_v2_projection_threads VALUES (?,?,?,?,?,?)", ("t-p-old", "OldP", "antigravity", "antigravity", None, iso_utc_ago(72.0)))
        c3.execute("INSERT INTO orchestration_v2_projection_provider_threads VALUES (?,?,?,?)", ("t-p-old", "antigravity", f"native-thread:{u_old_p}", json.dumps({"nativeThreadRef": {"driver": "antigravity", "nativeId": u_old_p}})))
        c3.execute("INSERT INTO orchestration_v2_projection_threads VALUES (?,?,?,?,?,?)", ("t-p-new", "NewP", "antigravity", "antigravity", None, iso_utc_ago(0.2)))
        c3.execute("INSERT INTO orchestration_v2_projection_provider_threads VALUES (?,?,?,?)", ("t-p-new", "antigravity", f"native-thread:{u_new_p}", json.dumps({"nativeThreadRef": {"driver": "antigravity", "nativeId": u_new_p}})))
        c3.commit()
        c3.close()
        old_ts = time.time() - 72 * 3600
        for uid in (u_old_p, u_new_p, u_child):
            f = acp3 / "conversations" / f"{uid}.db"
            f.write_bytes(b"Z" * 512)
            os.utime(f, (old_ts, old_ts))
        for p_uid in (u_old_p, u_new_p):
            sd = acp3 / "brain" / p_uid / ".system_generated" / "subagents"
            sd.mkdir(parents=True)
            (sd / f"{u_child}.json").write_text(json.dumps({"conversationId": u_child}))
            for rd, _ds, fs in os.walk(acp3 / "brain" / p_uid, topdown=False):
                for fn in fs:
                    os.utime(os.path.join(rd, fn), (old_ts, old_ts))
                os.utime(rd, (old_ts, old_ts))
        _, reps3 = mod.build_plan(t_r03, include_superseded=False, include_unknown=False, min_age_hours=24, skip_lsof=True)
        by3 = {s.uuid: s for s in reps3[0].sessions}
        self.assertTrue(by3[u_child].too_recent, "Subagent reachable from old parent first & new parent second must use newer deletion time")

        # 3. R04: mtime UNCHANGED, only deleted_at becomes recent after preview -> blocked by cooldown check
        t_r04a = self.root / "t_r22_r04a"
        _, del4a = make_fixture(t_r04a, del_sizes=(4096,), del_age_hours=72.0)
        _, reps4a = mod.build_plan(t_r04a, include_superseded=False, include_unknown=False, min_age_hours=24, skip_lsof=True)
        s4a = next(s for s in reps4a[0].sessions if s.uuid == del4a[0])
        self.assertFalse(s4a.too_recent)
        c4a = sqlite3.connect(t_r04a / "userdata" / "statev2.sqlite")
        c4a.execute("UPDATE orchestration_v2_projection_threads SET deleted_at = ? WHERE thread_id = 't-del-0'", (iso_utc_ago(0.1),))
        c4a.commit()
        c4a.close()
        app4a, blk4a = mod.reverify_before_execute(t_r04a, mod.Translator("en"), [s4a], skip_lsof=True, min_age_hours=24.0)
        self.assertEqual(app4a, [])
        self.assertEqual(len(blk4a), 1)
        self.assertIn("cooldown", blk4a[0][1].lower())

        # 4. R04: main() must pass --min-age through to execute_cleanup (10h-old session with --min-age 1)
        t_r04b = self.root / "t_r22_r04b"
        acps4b, del4b = make_fixture(t_r04b, del_sizes=(4096,), del_age_hours=10.0)
        r4b = subprocess.run(
            [str(SCRIPT), "--t3-home", str(t_r04b), "--apply", "-y", "--min-age", "1", "--skip-lsof"],
            env=self.env, capture_output=True, text=True,
        )
        self.assertEqual(r4b.returncode, 0, r4b.stdout + r4b.stderr)
        self.assertFalse((acps4b[0] / "conversations" / f"{del4b[0]}.db").exists())

        # 5. R07 execution stage: statev2.sqlite lsof error skips index prune even if acp_dir lsof succeeds
        t_r07 = self.root / "t_r22_r07"
        orphans = ("33333333-3333-3333-3333-000000000001",)
        acps7, _ = make_fixture(t_r07, del_sizes=(), orphan_ids=orphans)
        _, reps7 = mod.build_plan(t_r07, include_superseded=False, include_unknown=False, min_age_hours=24, skip_lsof=True)
        out7, err7 = io.StringIO(), io.StringIO()
        with mock.patch.object(mod, "inspect_file_open", return_value=(False, "simulated statev2 lsof error")):
            with mock.patch.object(mod, "inspect_open_handles", return_value=(set(), False, None)):
                with redirect_stdout(out7), redirect_stderr(err7):
                    mod.execute_cleanup(t_r07, reps7, [], prune_summaries=True, skip_lsof=False, st=mod.Style(False), tr=mod.Translator("en"))
        c7 = sqlite3.connect(acps7[0] / "conversation_summaries.db")
        self.assertEqual(c7.execute("SELECT COUNT(*) FROM conversation_summaries").fetchone()[0], 2)
        c7.close()

        # 6. R07: build_menu_snapshot treats t3_lsof_error as busy even when t3_db_open is False
        rep_lsof_err = mod.ProfileReport(
            profile_name="aaaa", acp_dir=acps7[0], t3_db_open=False, t3_lsof_error="lsof failed"
        )
        self.assertTrue(mod.build_menu_snapshot([rep_lsof_err]).profiles_busy)

        # 7. R11: reverify general Exception returns 1 without traceback
        out11, err11 = io.StringIO(), io.StringIO()
        with mock.patch.object(mod, "reverify_before_execute", side_effect=RuntimeError("boom")):
            with redirect_stdout(out11), redirect_stderr(err11):
                rc11 = mod.execute_cleanup(t_r07, reps7, [], prune_summaries=False, skip_lsof=True, st=mod.Style(False), tr=mod.Translator("en"))
        self.assertEqual(rc11, 1)
        self.assertNotIn("Traceback", err11.getvalue())
        self.assertIn("boom", err11.getvalue())

    def test_r24_staging_ignores_ds_store_only(self):
        t = self.root / "t_r24"
        orphans = ("33333333-3333-3333-3333-000000000001",)
        acps, _ = make_fixture(t, del_sizes=(), orphan_ids=orphans)
        staging = acps[0].parent / ".t3-clean-staging"
        staging.mkdir()
        (staging / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")

        # (1) Only .DS_Store regular file -> no warning, does not block index pruning
        _, reps = mod.build_plan(t, include_superseded=False, include_unknown=False, min_age_hours=24, skip_lsof=True)
        self.assertEqual(reps[0].staging_leftovers, [])
        r_dry = subprocess.run([str(SCRIPT), "--t3-home", str(t), "-n", "--skip-lsof"], env=self.env, capture_output=True, text=True)
        self.assertNotIn(".DS_Store", r_dry.stdout)
        self.assertNotIn(".t3-clean-staging", r_dry.stdout)

        r_prune = subprocess.run(
            [str(SCRIPT), "--t3-home", str(t), "--apply", "-y", "--prune-summaries", "--skip-lsof"],
            env=self.env, capture_output=True, text=True,
        )
        self.assertEqual(r_prune.returncode, 0, r_prune.stdout + r_prune.stderr)
        c = sqlite3.connect(acps[0] / "conversation_summaries.db")
        self.assertEqual(c.execute("SELECT COUNT(*) FROM conversation_summaries").fetchone()[0], 1)
        c.close()

        # (2) .DS_Store + leftover bundle & unknown file -> warns ONLY on real leftovers and blocks index pruning
        c = sqlite3.connect(acps[0] / "conversation_summaries.db")
        c.execute("INSERT INTO conversation_summaries VALUES (?,?,?,?,?)", (orphans[0], "", 0, "0001-01-01 00:00:00", ""))
        c.commit()
        c.close()
        bundle = staging / "t3-session-cleaner-leftover"
        bundle.mkdir()
        (bundle / "restore.sh").write_text("#!/usr/bin/env bash\n")
        other = staging / "unknown.dat"
        other.write_bytes(b"data")
        _, reps2 = mod.build_plan(t, include_superseded=False, include_unknown=False, min_age_hours=24, skip_lsof=True)
        self.assertEqual(reps2[0].staging_leftovers, sorted([bundle, other]))
        r_dry2 = subprocess.run([str(SCRIPT), "--t3-home", str(t), "-n", "--skip-lsof"], env=self.env, capture_output=True, text=True)
        self.assertIn(str(bundle), r_dry2.stdout)
        self.assertIn(str(other), r_dry2.stdout)
        self.assertNotIn(".DS_Store", r_dry2.stdout)
        r_skip = subprocess.run(
            [str(SCRIPT), "--t3-home", str(t), "--apply", "-y", "--prune-summaries", "--skip-lsof"],
            env=self.env, capture_output=True, text=True,
        )
        self.assertEqual(r_skip.returncode, 0)
        c = sqlite3.connect(acps[0] / "conversation_summaries.db")
        self.assertEqual(c.execute("SELECT COUNT(*) FROM conversation_summaries").fetchone()[0], 2)
        c.close()

        # (3) Directory named .DS_Store is NOT a regular file -> still treated as leftover
        other.unlink()
        (bundle / "restore.sh").unlink()
        bundle.rmdir()
        (staging / ".DS_Store").unlink()
        (staging / ".DS_Store").mkdir()
        _, reps3 = mod.build_plan(t, include_superseded=False, include_unknown=False, min_age_hours=24, skip_lsof=True)
        self.assertEqual(reps3[0].staging_leftovers, [staging / ".DS_Store"])

        # (4) Symlink named .DS_Store pointing to a file -> still treated as leftover
        (staging / ".DS_Store").rmdir()
        target_file = self.root / "ds_target.bin"
        target_file.write_bytes(b"payload")
        (staging / ".DS_Store").symlink_to(target_file)
        _, reps4 = mod.build_plan(t, include_superseded=False, include_unknown=False, min_age_hours=24, skip_lsof=True)
        self.assertEqual(reps4[0].staging_leftovers, [staging / ".DS_Store"])

    def test_tui_deep_clean_syncs_summaries_and_returns_to_refreshed_menu(self):
        # Cooldown deleted session has a live row in summaries.db PLUS 2 pre-existing stale orphan rows.
        # Deep clean while T3 is closed must trash the session AND remove ONLY its matching summaries.db row
        # (leaving the 2 pre-existing stale orphan rows untouched for the dedicated Prune index action!),
        # then return directly to the refreshed Main Menu instead of exiting.
        t = self.root / "t_tui_deep_sync"
        pre_orphans = (
            "33333333-3333-3333-3333-000000000001",
            "33333333-3333-3333-3333-000000000002",
        )
        acps, del_ids = make_fixture(t, del_sizes=(4096,), del_age_hours=2.0, orphan_ids=pre_orphans)
        target_db = acps[0] / "conversations" / f"{del_ids[0]}.db"

        rc, caps = run_pty(
            [str(SCRIPT), "--t3-home", str(t), "--skip-lsof", "--lang", "zh"],
            self.env,
            cols=92,
            rows=26,
            steps=[
                (b"\r", "同步清理对应的 1 条 summaries.db 索引"),  # Cursor on 彻底清理 -> only 1 matching row, not 3!
                (b"y", "2 · 可清理"),                              # Trash session + remove 1 matching row -> 2 pre-existing orphans intact!
                (b"\r", "同步清理 summaries.db 约 2 条失效索引"),    # Cursor moved to 清理索引 -> confirm pruning the 2 pre-existing orphans
                (b"y", "已干净"),                                  # Prune 2 pre-existing orphans -> refreshed Main Menu shows 已干净
                b"q",                                             # Quit TUI -> scrollback prints bundle & backup names
            ],
        )
        self.assertEqual(rc, 0)
        home0 = strip_ansi(caps[0].split("\x1b[H")[-1])
        self.assertIn("全部已删、旧版会话及对应索引", home0)
        self.assertBoxAligned(caps[0], 92, "deep-home-zh")

        conf = strip_ansi(caps[1].split("\x1b[H")[-1])
        self.assertIn("同步清理对应的 1 条 summaries.db 索引", conf)
        self.assertBoxAligned(caps[1], 92, "deep-confirm-zh")

        home_mid = strip_ansi(caps[2].split("\x1b[H")[-1])
        self.assertIn("无残留文件", home_mid)
        self.assertIn("2 · 可清理", home_mid)
        self.assertIn("并同步清理 1 条索引", home_mid)
        self.assertBoxAligned(caps[2], 92, "deep-mid-zh")

        home_end = strip_ansi(caps[4].split("\x1b[H")[-1])
        self.assertIn("已干净", home_end)
        self.assertBoxAligned(caps[4], 92, "deep-end-zh")

        exit_out = strip_ansi(caps[5] + caps[6])
        self.assertIn("t3-session-cleaner-2", exit_out)
        self.assertIn("t3-session-cleaner-summaries-backup-", exit_out)

        self.assertFalse(target_db.exists())
        c = sqlite3.connect(acps[0] / "conversation_summaries.db")
        rows = [r[0] for r in c.execute("SELECT conversation_id FROM conversation_summaries").fetchall()]
        c.close()
        self.assertEqual(rows, ["11111111-1111-1111-1111-111111111111"])
        self.assertEqual(len(list(self.trash_dir.glob("t3-session-cleaner-2*"))), 1)
        self.assertEqual(len(list(self.trash_dir.glob("t3-session-cleaner-summaries-backup-*"))), 2)

    def test_tui_confirm_p_toggle_and_multi_step_cleanup_in_one_session(self):
        # 1 old deleted session (>24h) + 1 cooldown deleted session (<24h), both in summaries.db.
        # Step 1: Standard clean with 'p' toggled OFF -> leaves 1 stale summary row and returns to menu.
        # Step 2: Deep clean in the SAME TUI session -> cleans cooldown session & ONLY its 1 matching row
        #         (the 1 stale row skipped in Step 1 stays untouched as 1 · Ready).
        # Step 3: Prune index in the SAME TUI session -> cleans that remaining 1 stale row.
        t = self.root / "t_tui_multistep"
        acps, del_old = make_fixture(t, del_sizes=(4096,), del_age_hours=72.0)
        acp = acps[0]
        u_cd = "22222222-2222-2222-2222-000000000099"
        c_t3 = sqlite3.connect(t / "userdata" / "statev2.sqlite")
        c_t3.execute("INSERT INTO orchestration_v2_projection_threads VALUES (?,?,?,?,?,?)", ("t-cd", "Cooldown", "antigravity", "antigravity", None, iso_utc_ago(1.0)))
        c_t3.execute("INSERT INTO orchestration_v2_projection_provider_threads VALUES (?,?,?,?)", ("t-cd", "antigravity", f"native-thread:{u_cd}", json.dumps({"nativeThreadRef": {"driver": "antigravity", "nativeId": u_cd}})))
        c_t3.commit()
        c_t3.close()
        f_cd = acp / "conversations" / f"{u_cd}.db"
        f_cd.write_bytes(b"C" * 2048)
        sc = sqlite3.connect(acp / "conversation_summaries.db")
        sc.execute("INSERT INTO conversation_summaries VALUES (?,?,?,?,?)", (u_cd, "Cooldown", 2, "2026-11-26", ""))
        sc.commit()
        sc.close()

        rc, caps = run_pty(
            [str(SCRIPT), "--t3-home", str(t), "--skip-lsof", "--lang", "en"],
            self.env,
            cols=92,
            rows=26,
            steps=[
                (b"\r", "Also remove 1 matching summaries.db"),    # Standard clean -> Confirm screen (index sync ON: 1)
                (b"p", "Skip 1 matching summaries.db"),            # Toggle index sync OFF
                (b"y", "1 · Ready"),                               # File-only clean -> Main Menu (cursor moves to Deep clean)
                (b"\r", "Also remove 1 matching summaries.db"),    # Deep clean -> ONLY its 1 matching row (not 2!)
                (b"y", "removed 1 index"),                         # Deep clean removes 1 matching row; Step 1's orphan remains!
                (b"\r", "Prune ~1 stale index"),                   # Cursor moved to Prune index -> confirm pruning leftover orphan
                (b"y", "Pruned 1 stale index"),                    # Prune leftover orphan -> Clean
                b"q",                                              # Quit
            ],
        )
        self.assertEqual(rc, 0)
        self.assertIn("Also remove 1 matching summaries.db", strip_ansi(caps[1].split("\x1b[H")[-1]))
        self.assertIn("Skip 1 matching summaries.db", strip_ansi(caps[2].split("\x1b[H")[-1]))
        self.assertIn("1 · Ready", strip_ansi(caps[3].split("\x1b[H")[-1]))
        self.assertIn("Also remove 1 matching summaries.db", strip_ansi(caps[4].split("\x1b[H")[-1]))
        home_after_deep = strip_ansi(caps[5].split("\x1b[H")[-1])
        self.assertIn("1 · Ready", home_after_deep)
        self.assertIn("removed 1 index", home_after_deep)
        self.assertIn("Pruned 1 stale index", strip_ansi(caps[7].split("\x1b[H")[-1]))

        self.assertFalse((acp / "conversations" / f"{del_old[0]}.db").exists())
        self.assertFalse(f_cd.exists())
        sc = sqlite3.connect(acp / "conversation_summaries.db")
        self.assertEqual(sc.execute("SELECT COUNT(*) FROM conversation_summaries").fetchone()[0], 1)
        sc.close()

    def test_tui_busy_skips_summaries_then_refresh_and_error_result_screen(self):
        # Part A: T3 running (statev2.sqlite open) -> Deep clean honestly skips summaries.db,
        # returns to menu; once T3 exits, pressing 'r' unlocks Prune index right inside the same TUI!
        t = self.root / "t_tui_busy"
        acps, del_ids = make_fixture(t, del_sizes=(4096,), del_age_hours=2.0)
        state_db = t / "userdata" / "statev2.sqlite"

        holder = subprocess.Popen(
            [sys.executable, "-c", f"import sys, time; f = open({str(state_db)!r}, 'rb'); print('READY', flush=True); time.sleep(30)"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            self.assertEqual(holder.stdout.readline().strip(), "READY")

            def stop_holder(_proc, _master):
                holder.terminate()
                holder.wait(timeout=3)

            rc, caps = run_pty(
                [str(SCRIPT), "--t3-home", str(t), "--lang", "en"],
                self.env,
                cols=92,
                rows=26,
                steps=[
                    (b"\r", "T3 running: 1 matching index row(s) kept"),  # Deep clean -> Confirm shows T3 running notice
                    (b"y", "1 · T3 running"),                             # Trash file only -> Main Menu (1 · T3 running)
                    stop_holder,                                          # Quit T3
                    (b"r", "1 · Ready"),                                  # Live refresh -> Prune index becomes Ready
                    (b"\x1b[A\x1b[A\r", "Prune ~1 stale index"),          # Move from Details (4) up 2 to Prune index (2) -> Confirm
                    (b"y", "Pruned 1 stale index"),                       # Prune index -> refreshed Main Menu (Clean)
                    b"q",                                                 # Quit
                ],
            )
        finally:
            if holder.poll() is None:
                holder.terminate()
                holder.wait(timeout=3)
            holder.stdout.close()
            holder.stderr.close()

        self.assertEqual(rc, 0)
        self.assertIn("T3 running: 1 matching index row(s) kept", strip_ansi(caps[1].split("\x1b[H")[-1]))
        self.assertIn("1 · T3 running", strip_ansi(caps[2].split("\x1b[H")[-1]))
        self.assertIn("1 · Ready", strip_ansi(caps[4].split("\x1b[H")[-1]))
        self.assertIn("Pruned 1 stale index", strip_ansi(caps[6].split("\x1b[H")[-1]))
        self.assertFalse((acps[0] / "conversations" / f"{del_ids[0]}.db").exists())
        sc = sqlite3.connect(acps[0] / "conversation_summaries.db")
        self.assertEqual(sc.execute("SELECT COUNT(*) FROM conversation_summaries").fetchone()[0], 1)
        sc.close()

        # Part B: Runtime failure in TUI shows aligned Cleanup Result screen, returns to menu on Enter,
        # and exits 1 when user quits.
        t_err = self.root / "t_tui_err"
        acps_e, del_e = make_fixture(t_err, del_sizes=(4096,), del_age_hours=72.0)
        fail_stub = make_stub_trash(self.root / "bin", self.trash_dir, fail=True)
        rc_e, caps_e = run_pty(
            [str(SCRIPT), "--t3-home", str(t_err), "--skip-lsof", "--lang", "en"],
            {**self.env, "T3_CLEAN_TRASH_BIN": str(fail_stub)},
            cols=74,
            rows=24,
            steps=[
                (b"\r", "Confirm Cleanup"),     # Standard clean -> Confirm
                (b"y", "Cleanup Failed"),       # Execute -> trash fails -> Cleanup Failed result screen
                (b"\r", "encountered error"),   # Dismiss result screen -> back to Main Menu
                b"q",                           # Quit -> exit 1
            ],
        )
        self.assertEqual(rc_e, 1)
        self.assertIn("Cleanup Failed", strip_ansi(caps_e[2].split("\x1b[H")[-1]))
        self.assertBoxAligned(caps_e[2], 74, "result-err-74-en")
        self.assertIn("encountered error", strip_ansi(caps_e[3].split("\x1b[H")[-1]))
        self.assertTrue((acps_e[0] / "conversations" / f"{del_e[0]}.db").exists())

    def test_superseded_transfer_and_handoff_are_terminal_while_active_ones_protect(self):
        # Deleted fork sessions referenced only by terminal 'superseded' transfer/handoff rows
        # must remain DELETED (cleanable), whereas non-terminal pending/resolved_native transfers,
        # pending handoffs, and pending outbox items must stay protected as ACTIVE with localized notes.
        t = self.root / "t_transfer_superseded"
        acps, del_ids = make_fixture(t, del_sizes=(4096, 4096, 4096, 4096, 4096), del_age_hours=72.0)
        u_sup_xfer, u_sup_ho, u_pend_xfer, u_pend_ho, u_pend_out = del_ids

        conn = sqlite3.connect(t / "userdata" / "statev2.sqlite")
        # Terminal superseded transfer (t-del-0 -> t-act) & superseded handoff (t-act referencing u_sup_ho)
        conn.execute(
            "INSERT INTO orchestration_v2_projection_context_transfers VALUES (?,?,?,?)",
            ("t-del-0", "t-act", "superseded", json.dumps({"sourcePoint": {"providerThreadRef": {"nativeId": u_sup_xfer}}})),
        )
        conn.execute(
            "INSERT INTO orchestration_v2_projection_context_handoffs VALUES (?,?,?)",
            ("t-act", "superseded", json.dumps({"summaryText": f"old handoff {u_sup_ho}"})),
        )
        # Non-terminal: superseded BEFORE pending for u_pend_xfer must skip superseded and protect via pending
        conn.execute(
            "INSERT INTO orchestration_v2_projection_context_transfers VALUES (?,?,?,?)",
            ("t-del-2", "t-act", "superseded", json.dumps({"sourcePoint": {"providerThreadRef": {"nativeId": u_pend_xfer}}})),
        )
        conn.execute(
            "INSERT INTO orchestration_v2_projection_context_transfers VALUES (?,?,?,?)",
            ("t-del-2", "t-act", "pending", json.dumps({"sourcePoint": {"providerThreadRef": {"nativeId": u_pend_xfer}}})),
        )
        conn.execute(
            "INSERT INTO orchestration_v2_projection_context_handoffs VALUES (?,?,?)",
            ("t-act", "pending", json.dumps({"summaryText": f"active handoff {u_pend_ho}"})),
        )
        conn.execute(
            "INSERT INTO orchestration_v2_effect_outbox VALUES (?,?,?)",
            ("t-act", "pending", json.dumps({"nativeThreadId": u_pend_out})),
        )
        conn.commit()
        conn.close()

        _, reps = mod.build_plan(t, include_superseded=False, include_unknown=False, min_age_hours=24, skip_lsof=True)
        by_id = {s.uuid: s for s in reps[0].sessions}

        # Superseded-only rows stay DELETED & selectable
        self.assertEqual(by_id[u_sup_xfer].category, "DELETED")
        self.assertTrue(by_id[u_sup_xfer].selected)
        self.assertEqual(by_id[u_sup_ho].category, "DELETED")
        self.assertTrue(by_id[u_sup_ho].selected)

        # Non-terminal rows remain protected as ACTIVE with localized notes (no raw English status leak)
        tr_zh, tr_en = mod.Translator("zh"), mod.Translator("en")
        self.assertEqual(by_id[u_pend_xfer].category, "ACTIVE")
        self.assertEqual(mod.session_note(by_id[u_pend_xfer], tr_zh), "上下文待传")
        self.assertEqual(mod.session_note(by_id[u_pend_xfer], tr_en), "Transfer")

        self.assertEqual(by_id[u_pend_ho].category, "ACTIVE")
        self.assertEqual(mod.session_note(by_id[u_pend_ho], tr_zh), "上下文交接")
        self.assertEqual(mod.session_note(by_id[u_pend_ho], tr_en), "Handoff")

        self.assertEqual(by_id[u_pend_out].category, "ACTIVE")
        self.assertEqual(mod.session_note(by_id[u_pend_out], tr_zh), "待执行任务")
        self.assertEqual(mod.session_note(by_id[u_pend_out], tr_en), "Queued task")

        # Also verify resolved_native stays protected (non-terminal until consumed)
        conn = sqlite3.connect(t / "userdata" / "statev2.sqlite")
        conn.execute(
            "INSERT INTO orchestration_v2_projection_context_transfers VALUES (?,?,?,?)",
            ("t-del-0", "t-act", "resolved_native", json.dumps({"sourcePoint": {"providerThreadRef": {"nativeId": u_sup_xfer}}})),
        )
        conn.commit()
        conn.close()
        _, reps2 = mod.build_plan(t, include_superseded=False, include_unknown=False, min_age_hours=24, skip_lsof=True)
        by_id2 = {s.uuid: s for s in reps2[0].sessions}
        self.assertEqual(by_id2[u_sup_xfer].category, "ACTIVE")
        self.assertEqual(mod.session_note(by_id2[u_sup_xfer], tr_zh), "上下文待传")

    def test_ctrl_c_exits_without_traceback(self):
        t = self.root / "t_ctrl_c"
        make_fixture(t)

        def ctrl_c(proc, _master):
            proc.send_signal(signal.SIGINT)

        rc, caps = run_pty(
            [str(SCRIPT), "--t3-home", str(t), "-i", "--skip-lsof", "--lang", "en"],
            self.env,
            cols=92,
            rows=26,
            steps=[
                (b"\x1b[B\x1b[B\x1b[B\x1b[B\r", "All Local Sessions"),
                ctrl_c,  # Sub-page: back to Main Menu
                ctrl_c,  # Main Menu: quit
            ],
        )
        self.assertEqual(rc, 0)
        self.assertIn("All Local Sessions", strip_ansi(caps[1]))
        self.assertIn("Standard clean", strip_ansi(caps[2].split("\x1b[H")[-1]))
        self.assertIn("Exited without changing any files.", strip_ansi("".join(caps)))
        self.assertNotIn("Traceback", "".join(caps))

        # Interrupted while scanning (outside the TUI key loop).
        started = self.root / "lsof_started"
        slow_lsof = self.root / "slow_bin" / "lsof"
        slow_lsof.parent.mkdir()
        slow_lsof.write_text(f"#!/bin/sh\n: > {str(started)!r}\nexec sleep 30\n")
        slow_lsof.chmod(0o755)
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPT), "--t3-home", str(t), "-n"],
            env={**self.env, "PATH": f"{slow_lsof.parent}:{self.env['PATH']}"},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        deadline = time.time() + 10
        while not started.exists() and time.time() < deadline:
            time.sleep(0.05)
        proc.send_signal(signal.SIGINT)
        _, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 130)
        self.assertNotIn("Traceback", err)

    def test_cli_report_colon_matches_language(self):
        t = self.root / "t_colon"
        make_fixture(t)
        for lang, label in (("en", "Storage: "), ("zh", "空间变化：")):
            res = subprocess.run(
                [str(SCRIPT), "--t3-home", str(t), "-n", "--skip-lsof", "--lang", lang],
                env=self.env, capture_output=True, text=True,
            )
            self.assertIn(label, res.stdout)
            if lang == "en":
                self.assertNotIn("：", res.stdout)


if __name__ == "__main__":
    unittest.main()
