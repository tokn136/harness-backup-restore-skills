#!/usr/bin/env python3
"""Isolated tests with synthetic home folders; never touch the real VS Code data."""
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
import urllib.parse

spec = importlib.util.spec_from_file_location("vm", Path(__file__).with_name("vscode_migration.py"))
vm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vm)


def uri(path):
    return "file://" + urllib.parse.quote(str(path))


@unittest.skipUnless(shutil.which("git"), "git is required")
class VSCodeMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vscode-migration-test-")
        self.base = Path(self.temp.name).resolve()
        self.old, self.new = self.base / "old-user", self.base / "new-user"
        self.new.mkdir()
        self.app = self.old / "Library/Application Support/Code"
        self.user = self.app / "User"
        self.storage = self.user / "workspaceStorage"
        self.storage.mkdir(parents=True)
        (self.user / "settings.json").write_text(json.dumps(
            {"python.defaultInterpreterPath": str(self.old / "Developer/Local Notes/.venv/bin/python")}))
        (self.old / ".vscode/extensions").mkdir(parents=True)
        (self.old / ".vscode/argv.json").write_text("{}")
        (self.old / ".vscode/extensions/extensions.json").write_text(json.dumps(
            [{"identifier": {"id": "ms-python.python"}, "version": "2026.1.0"}]))
        (self.old / ".copilot/chats").mkdir(parents=True)
        (self.old / ".copilot/chats/one.json").write_text("{}")
        (self.old / ".copilot/logs").mkdir()
        (self.old / ".copilot/logs/big.log").write_text("x")

        # Local project without git: copied, rebuildable folders skipped, build/ kept.
        self.local = self.old / "Developer/Local Notes"
        (self.local / "build").mkdir(parents=True)
        (self.local / "build/report.txt").write_text("real work")
        (self.local / "node_modules/x").mkdir(parents=True)
        (self.local / "node_modules/x/i.js").write_text("rebuildable")
        (self.local / "notes.md").write_text("hello")
        # Pushed repo: linked; .env kept.
        self.remote = self.base / "remote.git"
        self.repo = self.old / "Developer/Linked"
        self.git("init", "--bare", "-b", "main", self.remote)
        self.git("init", "-b", "main", self.repo)
        (self.repo / ".gitignore").write_text(".env\nnode_modules/\n")
        (self.repo / "app.py").write_text("print(1)\n")
        self.git("-C", self.repo, "add", "-A")
        self.git("-C", self.repo, "commit", "-m", "init")
        self.git("-C", self.repo, "remote", "add", "origin", self.remote)
        self.git("-C", self.repo, "push", "-u", "origin", "main")
        (self.repo / ".env").write_text("TOKEN=local-only\n")
        (self.repo / "node_modules").mkdir()
        (self.repo / "node_modules/a.js").write_text("rebuildable")
        # Repo with uncommitted work: flagged and copied.
        self.dirty = self.old / "Developer/Dirty"
        self.git("init", "-b", "main", self.dirty)
        (self.dirty / "wip.txt").write_text("not committed")
        # Cloud folder: linked, not copied.
        self.cloud = self.old / "Library/CloudStorage/OneDrive-Test/House"
        self.cloud.mkdir(parents=True)
        (self.cloud / "plan.xlsx").write_text("synced")

        self.ids = {}
        for p in (self.local, self.repo, self.dirty, self.cloud):
            wid = vm.folder_id(p)
            self.ids[p] = wid
            (self.storage / wid / "chatSessions").mkdir(parents=True)
            (self.storage / wid / "workspace.json").write_text(json.dumps({"folder": uri(p)}))
            (self.storage / wid / "chatSessions/s.json").write_text(json.dumps({"said": "edit " + str(p) + "/x"}))
        (self.storage / "orphan").mkdir()
        (self.storage / "orphan/workspace.json").write_text(json.dumps({"folder": "file:///c%3A/dev/old"}))
        (self.user / "globalStorage").mkdir()
        con = sqlite3.connect(self.user / "globalStorage/state.vscdb")
        con.execute("CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
        con.execute("INSERT INTO ItemTable VALUES (?, ?)", ("history.recentlyOpenedPathsList",
                    json.dumps({"entries": [{"folderUri": uri(self.local)}, {"folderUri": uri(self.old / "Developer/Linked")}]})))
        con.commit()
        con.close()
        self.archive = self.base / "out/vscode-backup.tar.gz"
        self.out = io.StringIO()

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        cmd = ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", *map(str, args)]
        return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout

    def run_cli(self, *argv):
        with contextlib.redirect_stdout(self.out):
            vm.main([*map(str, argv), "--force"])

    def backup(self, *extra):
        self.run_cli("backup", "--home", self.old, "--output", self.archive, *extra)

    def restore(self, *extra):
        self.run_cli("restore", self.archive, "--home", self.new, "--skip-extensions", *extra)

    def newpath(self, p):
        return self.new / p.relative_to(self.old)

    def test_inventory_classifies_projects(self):
        report = vm.inventory(self.old)
        linked = {x["path"]: x["kind"] for x in report["linked"]}
        self.assertEqual(linked, {str(self.repo): "git", str(self.cloud): "cloud"})
        self.assertEqual([a["path"] for a in report["attention"]], [str(self.dirty)])
        copied = [r["path"] for r in report["roots"] if r["kind"] == "project"]
        self.assertEqual(sorted(copied), sorted([str(self.local), str(self.dirty)]))
        self.assertIn(str(self.repo / ".env"), [r["path"] for r in report["roots"]])
        self.assertEqual(report["flavors"][0]["extensions"], ["ms-python.python@2026.1.0"])
        self.assertEqual([m["path"] for m in report["missing_projects"]], ["/c:/dev/old"])

    def test_roundtrip_relinks_and_refiles_chat_history(self):
        self.backup()
        names = tarfile.open(self.archive).getnames()
        self.assertFalse(any("node_modules" in n or "app.py" in n or "plan.xlsx" in n or ".copilot/logs" in n
                             for n in names))
        self.restore("--apply")
        repo, local, dirty = map(self.newpath, (self.repo, self.local, self.dirty))
        self.assertEqual((repo / "app.py").read_text(), "print(1)\n")
        self.assertEqual((repo / ".env").read_text(), "TOKEN=local-only\n")
        self.assertEqual(self.git("-C", repo, "status", "--porcelain"), "")
        self.assertEqual((local / "build/report.txt").read_text(), "real work")
        self.assertFalse((local / "node_modules").exists())
        self.assertEqual((dirty / "wip.txt").read_text(), "not committed")
        self.assertFalse(self.newpath(self.cloud).exists())
        self.assertIn("Sign in to OneDrive-Test", self.out.getvalue())
        storage = self.newpath(self.storage)
        # Chat history now sits under the ID VS Code computes for the new folders.
        for p in (repo, local, dirty):
            folder = storage / vm.folder_id(p)
            self.assertEqual(json.loads((folder / "workspace.json").read_text())["folder"], uri(p))
            self.assertIn(str(self.old), (folder / "chatSessions/s.json").read_text())  # history untouched
        self.assertTrue((storage / self.ids[self.cloud]).exists())  # waits for OneDrive
        settings = json.loads((self.newpath(self.user) / "settings.json").read_text())
        self.assertTrue(settings["python.defaultInterpreterPath"].startswith(str(self.new) + "/"))
        con = sqlite3.connect(self.newpath(self.user) / "globalStorage/state.vscdb")
        value = con.execute("SELECT value FROM ItemTable").fetchone()[0]
        con.close()
        value = value.decode() if isinstance(value, bytes) else value
        self.assertIn(uri(local), value)
        self.assertNotIn(str(self.old), value)
        self.assertTrue((self.new / ".copilot/chats/one.json").exists())

    def test_relink_after_cloud_sync_refiles_data(self):
        self.backup()
        self.restore("--apply")
        cloud = self.newpath(self.cloud)
        cloud.mkdir(parents=True)  # OneDrive finished syncing
        self.run_cli("relink", self.archive, "--home", self.new, "--apply", "--skip-extensions")
        self.assertTrue((self.newpath(self.storage) / vm.folder_id(cloud) / "chatSessions").exists())

    def test_encrypted_roundtrip(self):
        self.archive = self.base / "out/vscode-backup.tar.gz.enc"
        os.environ["VSCODE_MIGRATION_PASSWORD"] = "test-password"
        try:
            self.backup("--encrypt")
            self.assertFalse(tarfile.is_tarfile(self.archive))
            self.restore("--apply")
        finally:
            del os.environ["VSCODE_MIGRATION_PASSWORD"]
        self.assertTrue((self.newpath(self.local) / "notes.md").exists())

    def test_preview_changes_nothing(self):
        self.backup()
        self.restore()
        self.assertEqual(sorted(p.name for p in self.new.iterdir()), [])

    def test_rollback_restores_previous_state(self):
        self.backup()
        existing = self.newpath(self.user)
        existing.mkdir(parents=True)
        (existing / "settings.json").write_text('{"fresh": true}')
        self.restore("--apply")
        journal = next((self.new / ".vscode-migration-transactions").glob("*-restore.json"))
        self.run_cli("rollback", journal, "--apply")
        self.assertEqual((existing / "settings.json").read_text(), '{"fresh": true}')
        self.assertFalse(self.newpath(self.repo).exists())

    def test_tampered_archive_is_rejected(self):
        self.backup()
        bad = self.base / "bad.tar.gz"
        with tarfile.open(self.archive) as src, tarfile.open(bad, "w:gz") as dst:
            for m in src.getmembers():
                data = src.extractfile(m).read() if m.isfile() else None
                if m.name.endswith("notes.md"):
                    data = b"changed"
                    m.size = len(data)
                dst.addfile(m, io.BytesIO(data) if data is not None else None)
            evil = tarfile.TarInfo("files/../../escape")
            evil.size = 1
            dst.addfile(evil, io.BytesIO(b"x"))
        with self.assertRaisesRegex(RuntimeError, "Unsafe"):
            self.run_cli("restore", bad, "--home", self.new, "--apply", "--skip-extensions")
        self.assertFalse((self.base / "escape").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
