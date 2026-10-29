#!/usr/bin/env python3
"""Isolated regression tests; never reads or restores the user's Codex home."""
import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("migration", Path(__file__).with_name("migration.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-migration-test-")
        self.base = Path(self.temp.name).resolve()
        self.old = self.base / "old-user"
        self.new = self.base / "new-user"
        self.ch = self.old / ".codex"
        self.project = self.old / "Developer" / "Project with spaces"
        self.rollout = self.ch / "sessions" / "rollout.jsonl"
        for p in (self.ch, self.project, self.old / "Documents/Codex/task/outputs", self.new):
            p.mkdir(parents=True, exist_ok=True)
        self.rollout.parent.mkdir()
        self.rollout.write_text(json.dumps({"type": "session_meta", "payload": {"cwd": str(self.project)}}) + "\n" +
                                json.dumps({"type": "event_msg", "payload": {"message": "Historical " + str(self.old)}}) + "\n")
        self.history_line = self.rollout.read_bytes().splitlines()[1]
        (self.ch / ".codex-global-state.json").write_text(json.dumps({"local-projects": {"p": {"rootPaths": [str(self.project)]}}}))
        (self.ch / "config.toml").write_text('model = "test"\n')
        (self.ch / "auth.json").write_text('{"synthetic":"not-a-real-credential"}')
        (self.project / ".env").write_text("SYNTHETIC=test\n")
        (self.project / "untracked-ü.txt").write_text("uncommitted contents\n")
        (self.project / "executable.sh").write_text("#!/bin/sh\nexit 0\n")
        (self.project / "executable.sh").chmod(0o751)
        (self.project / "absolute-link").symlink_to(self.project / "untracked-ü.txt")
        self.has_xattr = False
        try:
            os.setxattr(self.project / "untracked-ü.txt", "user.codex_migration_test", b"test-value")
            self.has_xattr = True
        except (AttributeError, OSError):
            pass
        (self.old / "Documents/Codex/task/outputs/result.txt").write_text("artifact\n")
        browser = self.old / "Library/Application Support/Codex"
        browser.mkdir(parents=True)
        (browser / "Local State").write_text('{"synthetic":true}')
        (browser / "SingletonLock").symlink_to("old-machine-123")
        self.db = sqlite3.connect(self.ch / "state_5.sqlite")
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE threads(id TEXT PRIMARY KEY,cwd TEXT,rollout_path TEXT,archived INTEGER,sandbox_policy TEXT);
            CREATE TABLE project_roots(project_id TEXT,path TEXT);
            CREATE TABLE projects(id TEXT,metadata TEXT);
            CREATE TABLE thread_attachments(id TEXT,identity_key TEXT,payload TEXT);
        ''')
        self.db.execute("INSERT INTO threads VALUES(?,?,?,?,?)", ("t", str(self.project), str(self.rollout), 1, json.dumps({"writable_roots": [str(self.project)]})))
        self.db.execute("INSERT INTO project_roots VALUES(?,?)", ("p", str(self.project)))
        self.db.execute("INSERT INTO projects VALUES(?,?)", ("p", "{}"))
        self.db.commit()  # Keep connection open to exercise preserved SQLite WAL.
        self.archive = self.base / "archives" / "backup.tar.gz"
        self.output = io.StringIO()
        self.guard = patch.object(m, "require_stopped", return_value=None)
        self.version = patch.object(m, "app_version", return_value={"test": "1"})
        self.guard.start()
        self.version.start()

    def tearDown(self):
        self.db.close()
        self.guard.stop()
        self.version.stop()
        self.temp.cleanup()

    def backup(self, **kw):
        args = dict(home=str(self.old), codex_home=str(self.ch), include=[], output=str(self.archive), encrypt=False, allow_incomplete=False)
        args.update(kw)
        with contextlib.redirect_stdout(self.output):
            m.backup(argparse.Namespace(**args))
        return m.verify_plain(self.archive)

    def restore(self, apply=True, **kw):
        args = dict(archive=str(self.archive), map=[str(self.old) + "=" + str(self.new)], apply=apply, accept_version_mismatch=False)
        args.update(kw)
        with patch.object(m.Path, "home", return_value=self.new), contextlib.redirect_stdout(self.output):
            m.restore(argparse.Namespace(**args))

    def journal(self):
        return next((self.new / ".codex-migration-transactions").glob("*.json"))

    def test_roundtrip_with_wal_unicode_permissions_symlinks_and_history(self):
        manifest = self.backup()
        self.assertEqual(manifest["databases"][0]["archived_count"], 1)
        self.assertTrue(any(e["name"].endswith("state_5.sqlite-wal") for e in manifest["entries"]))
        self.assertFalse(any(e["name"].endswith("state_5.sqlite-shm") for e in manifest["entries"]))
        self.restore()
        project = self.new / self.project.relative_to(self.old)
        self.assertEqual((project / "untracked-ü.txt").read_text(), "uncommitted contents\n")
        self.assertEqual((project / "executable.sh").stat().st_mode & 0o777, 0o751)
        self.assertEqual(os.readlink(project / "absolute-link"), str(project / "untracked-ü.txt"))
        if self.has_xattr:
            self.assertEqual(os.getxattr(project / "untracked-ü.txt", "user.codex_migration_test"), b"test-value")
        with contextlib.closing(sqlite3.connect(self.new / ".codex/state_5.sqlite")) as con:
            self.assertEqual(con.execute("SELECT cwd FROM threads").fetchone()[0], str(project))
            self.assertEqual(con.execute("SELECT count(*) FROM threads").fetchone()[0], 1)
        lines = (self.new / ".codex/sessions/rollout.jsonl").read_bytes().splitlines()
        self.assertEqual(lines[1], self.history_line)
        self.assertEqual(json.loads(lines[0])["payload"]["cwd"], str(project))
        self.assertTrue((self.new / "Documents/Codex/task/outputs/result.txt").exists())

    def test_dry_run_does_not_create_destinations(self):
        self.backup()
        self.restore(apply=False)
        self.assertFalse((self.new / ".codex").exists())
        self.assertFalse((self.new / ".codex-migration-transactions").exists())

    def test_rollback_preserves_existing_and_new_data(self):
        self.backup()
        dest = self.new / ".codex"
        dest.mkdir()
        (dest / "old-destination.txt").write_text("preserve me")
        self.restore()
        (dest / "new-destination.txt").write_text("preserve this too")
        with contextlib.redirect_stdout(self.output):
            m.rollback(argparse.Namespace(journal=str(self.journal()), apply=True))
        self.assertEqual((dest / "old-destination.txt").read_text(), "preserve me")
        moved = list(self.new.glob(".codex.after-migration-*"))
        self.assertEqual((moved[0] / "new-destination.txt").read_text(), "preserve this too")
        self.assertFalse((self.new / "Documents/Codex").exists())

    def test_same_paths_do_not_change_chat_bytes(self):
        self.backup()
        self.db.close()
        with patch.object(m.Path, "home", return_value=self.old), contextlib.redirect_stdout(self.output):
            m.restore(argparse.Namespace(archive=str(self.archive), map=[], apply=True, accept_version_mismatch=False))
        self.assertEqual(self.rollout.read_bytes().splitlines()[1], self.history_line)
        with contextlib.closing(sqlite3.connect(self.ch / "state_5.sqlite")) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM threads").fetchone()[0], 1)
        self.db = sqlite3.connect(":memory:")

    def test_missing_paths_require_explicit_incomplete(self):
        self.db.execute("UPDATE threads SET cwd=?", (str(self.old / "missing"),))
        self.db.commit()
        with self.assertRaisesRegex(RuntimeError, "missing/broad"):
            self.backup()
        self.assertEqual(self.backup(allow_incomplete=True)["completeness"], "incomplete")

    def test_external_symlink_target_is_reported_and_can_be_included(self):
        outside = self.base / "external-dependency"
        outside.mkdir()
        (outside / "data").write_text("external")
        (self.project / "external").symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, "External symlink"):
            self.backup()
        manifest = self.backup(include=[str(outside)])
        self.assertIn(str(outside), manifest["roots"])
        self.assertFalse(manifest["external_symlinks"])

    def test_output_inside_source_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "outside all source"):
            self.backup(output=str(self.project / "backups/backup.tar.gz"))

    def test_destination_symlink_ancestor_is_rejected(self):
        self.backup()
        (self.new / "Developer").symlink_to(self.base)
        with self.assertRaisesRegex(RuntimeError, "symlink ancestor"):
            self.restore()
        self.assertFalse((self.new / ".codex").exists())

    def rewrite_archive(self, transform):
        altered = self.base / "altered.tar.gz"
        with tarfile.open(self.archive, "r:gz") as src, tarfile.open(altered, "w:gz") as dst:
            for member in src:
                content = src.extractfile(member).read() if member.isfile() else None
                member, content = transform(member, content)
                if content is not None:
                    member.size = len(content)
                dst.addfile(member, io.BytesIO(content) if content is not None else None)
        return altered

    def test_corruption_is_detected(self):
        self.backup()
        def corrupt(member, content):
            if member.name.endswith("untracked-ü.txt"):
                content = b"x" * len(content)
            return member, content
        with self.assertRaisesRegex(RuntimeError, "Checksum mismatch"):
            m.verify_plain(self.rewrite_archive(corrupt))

    def test_helper_corruption_is_detected(self):
        self.backup()
        def corrupt(member, content):
            return member, b"altered" if member.name == "migration.py" else content
        with self.assertRaisesRegex(RuntimeError, "Helper checksum"):
            m.verify_plain(self.rewrite_archive(corrupt))

    def test_path_traversal_is_rejected(self):
        self.backup()
        def corrupt(member, content):
            if member.name.endswith("untracked-ü.txt"):
                member.name = "../../outside"
                member.pax_headers.pop("path", None)
            return member, content
        with self.assertRaisesRegex(RuntimeError, "Unsafe"):
            m.verify_plain(self.rewrite_archive(corrupt))

    def test_version_mismatch_stops_before_writing(self):
        self.backup()
        with patch.object(m, "app_version", return_value={"test": "2"}):
            with self.assertRaisesRegex(RuntimeError, "original Codex version"):
                self.restore()
        self.assertFalse((self.new / ".codex").exists())

    def test_partial_commit_can_be_rolled_back(self):
        self.backup()
        (self.new / ".codex").mkdir()
        (self.new / ".codex/original").write_text("old")
        replace = os.replace
        def interrupt(src, dst):
            if str(src).endswith("/root") and Path(dst) == self.new / self.project.relative_to(self.old):
                raise OSError("simulated interrupted second install")
            return replace(src, dst)
        with patch.object(m.os, "replace", side_effect=interrupt):
            with self.assertRaisesRegex(OSError, "simulated"):
                self.restore()
        with contextlib.redirect_stdout(self.output):
            m.rollback(argparse.Namespace(journal=str(self.journal()), apply=True))
        self.assertEqual((self.new / ".codex/original").read_text(), "old")

    @unittest.skipUnless(shutil.which("git"), "git is required for linked-worktree coverage")
    def test_nested_git_worktree_and_common_repository_are_preserved(self):
        repo = self.old / "external-git-repo"
        def git(*args):
            return subprocess.run(["git", *map(str, args)], check=True, capture_output=True, text=True).stdout
        git("init", repo)
        (repo / "tracked").write_text("committed")
        git("-C", repo, "add", "tracked")
        git("-C", repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture")
        worktree = self.ch / "worktrees/managed/project"
        worktree.parent.mkdir(parents=True)
        git("-C", repo, "worktree", "add", "-b", "fixture-work", worktree)
        (worktree / "tracked").write_text("uncommitted changes")
        manifest = self.backup()
        self.assertIn(str(repo), manifest["roots"])
        self.restore()
        newtree = self.new / worktree.relative_to(self.old)
        self.assertIn(" M tracked", git("-C", newtree, "status", "--porcelain"))
        self.assertEqual(git("-C", newtree, "rev-parse", "HEAD"), git("-C", repo, "rev-parse", "HEAD"))

    def test_running_app_guard_blocks_backup(self):
        self.guard.stop()
        process = subprocess.CompletedProcess([], 0, stdout="12 /Applications/Codex.app/Contents/MacOS/Codex\n")
        try:
            with patch.object(m.subprocess, "run", return_value=process):
                with self.assertRaisesRegex(RuntimeError, "Quit Codex"):
                    self.backup()
        finally:
            self.guard.start()
        self.assertFalse(self.archive.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
