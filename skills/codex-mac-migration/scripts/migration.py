#!/usr/bin/env python3
"""Offline Codex Mac backup/restore. Python 3.9+, standard library; age optional."""
import argparse
import base64
from contextlib import closing
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid

FORMAT = 1
APP_DIRS = (
    "Library/Application Support/Codex",
    "Library/Application Support/com.openai.codex",
    "Library/Application Support/com.openai.codex.installer",
    "Library/Application Support/OpenAI/Codex",
    "Library/Preferences/com.openai.codex.plist",
    "Library/Preferences/com.openai.codex.installer.plist",
    "Library/Saved Application State/com.openai.codex.savedState",
    "Library/WebKit/com.openai.codex",
    "Library/HTTPStorages/com.openai.codex",
)
NOTES = [
    "Local filesystem backup only. Cloud-only chats, projects and hosted artifacts are not independently exported.",
    "Sign into the same account on the new Mac. Keychain secrets, browser OS encryption keys and macOS permissions are not migrated.",
    "The archive may contain auth.json, browser sessions, .env files, source code and private chats. Protect it; age encryption is available.",
    "Project folders include ignored/untracked files and .git. Remote computers, VM disks and external symlink targets are reported, not silently followed.",
    "Restore uses original absolute paths. A different home directory requires an explicit path mapping and inspection of remaining references.",
    "A running task or terminal process cannot be restored. Reinstall compatible Codex and required runtimes/tools on the new Mac.",
]
RESTORE_GUIDE = """# Restore a Codex Mac migration backup

Requirements: macOS, Python 3.9 or newer (`python3 --version`), compatible Codex
installed but completely quit. Python is not bundled with this archive. Use
the same macOS short username and the same Codex version where possible.
Install Python from python.org if needed. Reauthenticate the same account.

Keep an untouched copy of the archive. From Terminal, list its contents first:

    tar -tzf /path/to/codex-backup.tar.gz | head

Extract only the helpers into a new, empty folder (do not unpack payload yourself):

    mkdir codex-restore-tools
    cd codex-restore-tools
    tar -xzf /path/to/codex-backup.tar.gz migration.py RESTORE.md
    python3 migration.py verify /path/to/codex-backup.tar.gz
    python3 migration.py restore /path/to/codex-backup.tar.gz

The restore command previews by default. After checking the destinations:

    python3 migration.py restore /path/to/codex-backup.tar.gz --apply

Existing destination roots are moved aside, never merged. A rollback journal
is printed and saved in ~/.codex-migration-transactions. Keep it and the sibling
rollback folders until the new Mac has been verified. Quit Codex before undoing:

    python3 migration.py rollback /path/to/transaction.json --apply

For an age-encrypted archive, install age (age-encryption.org), decrypt locally
before extracting the helpers, or use an existing copy of migration.py:

    age -d -o codex-backup.tar.gz codex-backup.tar.gz.age

Verify/restore also accept .age directly and prompt in Terminal. Passwords are
never passed on the command line or saved by this tool. Decrypted temporary
files are removed on normal exit; disk encryption is recommended.

Different macOS home directory: use `--map /Users/old=/Users/new` on restore.
Only recognized operational fields in Codex state, SQLite indexes, rollout
session metadata, automation cwd fields, Git .git/commondir/gitdir pointers,
and absolute symlinks are remapped. Original chat messages are preserved.
Unknown schemas stop the restore rather than guessing. A reference scan reports
remaining old paths, which may be historical text or require manual correction.
Check custom CODEX_HOME/MCP settings and plugin-specific files yourself.

Do not enable Codex automations on both Macs. Review them before launching the
new installation and keep the old Mac offline or Codex quit during migration.

Open Codex and check active AND archived chats, pinned/sidebar organization,
project roots, attachments, generated documents, skills/plugins and worktrees.
Run `git status` in restored repositories. Reconnect integrations, reinstall
runtimes, grant macOS permissions and separately migrate external VMs as needed.
Cloud-only content requires the original account and service access; this file
does not constitute a cloud export. Do not erase the old Mac until checks pass.
"""


def fail(message):
    raise RuntimeError(message)


def absolute(value):
    return Path(os.path.abspath(os.path.expanduser(str(value))))


def within(path, parent):
    return path == parent or parent in path.parents


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".new")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def app_bundles():
    found = []
    for folder in (Path("/Applications"), Path.home() / "Applications"):
        for app in sorted(folder.glob("*.app")):
            try:
                d = plistlib.loads((app / "Contents/Info.plist").read_bytes())
            except (OSError, ValueError):
                continue
            if d.get("CFBundleIdentifier") == "com.openai.codex":
                found.append((app, d))
    return found


def app_version():
    found = app_bundles()
    if not found:
        return None
    _, d = found[0]
    return {k: d.get(k) for k in ("CFBundleIdentifier", "CFBundleShortVersionString", "CFBundleVersion")}


def require_stopped():
    try:
        p = subprocess.run(["ps", "-axo", "pid=,comm="], text=True, capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        fail("Cannot check running processes here. Run this helper from macOS Terminal after quitting Codex.")
    bundles = app_bundles()
    app_paths = [str(app).lower() + "/contents/" for app, _ in bundles]
    names = {str(d.get("CFBundleExecutable", "")).lower() for _, d in bundles}
    found = []
    for line in p.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        pid, command = parts
        name = Path(command).name.lower()
        if ("codex.app/contents/" in command.lower() or any(b in command.lower() for b in app_paths)
                or name in {"codex", "codex app"} | names):
            found.append(pid + " " + Path(command).name)
    if found:
        fail("Quit Codex, Codex CLI and their helpers first. Still running: " + ", ".join(found))


def connect_ro(path):
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)


def schema(con):
    return {name: [r[1] for r in con.execute('PRAGMA table_info("' + name.replace('"', '""') + '")')]
            for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def inventory(home, codex_home, includes=()):
    """Read only discovery. Never read credentials or message bodies for reporting."""
    roots = {}
    missing, warnings, dependencies, databases = [], [], [], []

    def add(value, reason, optional=False):
        p = absolute(value)
        if p == home or p == codex_home.parent and p != home or len(p.parts) < 3:
            warnings.append("Overly broad root requires a narrower --include: " + str(p))
            return
        if not p.exists() and not p.is_symlink():
            if not optional:
                missing.append({"path": str(p), "reason": reason})
            return
        roots.setdefault(str(p), set()).add(reason)

    add(codex_home, "Codex home")
    add(home / ".agents/skills", "Shared personal skills", True)
    add(home / "Documents/Codex", "Projectless work and outputs", True)
    for rel in APP_DIRS:
        add(home / rel, "macOS app data", True)
    for value in includes:
        add(value, "Explicit include")
    global_file = codex_home / ".codex-global-state.json"
    if global_file.exists():
        d = read_json(global_file)
        for project in d.get("local-projects", {}).values():
            for p in project.get("rootPaths", []):
                add(p, "Registered project")
        for key in ("thread-projectless-output-directories", "thread-workspace-root-hints"):
            for p in d.get(key, {}).values():
                if isinstance(p, str) and p.startswith("/"):
                    add(p, key)
        def archived_paths(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key.lower() in {"path", "archivepath", "archive_path", "workspacepath", "workspacedirectory", "root"} and isinstance(item, str) and item.startswith("/"):
                        add(item, "Managed worktree archive")
                    elif isinstance(item, (dict, list)):
                        archived_paths(item)
            elif isinstance(value, list):
                for item in value:
                    archived_paths(item)
        archived_paths(d.get("electron-managed-worktree-archives", []))
    for db in sorted(codex_home.glob("state_*.sqlite")):
        with closing(connect_ro(db)) as con:
            tables = schema(con)
            item = {"path": str(db), "tables": tables}
            if {"cwd", "rollout_path"} <= set(tables.get("threads", [])):
                item["thread_count"] = con.execute("SELECT count(*) FROM threads").fetchone()[0]
                if "archived" in tables["threads"]:
                    item["archived_count"] = con.execute("SELECT count(*) FROM threads WHERE archived=1").fetchone()[0]
                for cwd, rollout in con.execute("SELECT DISTINCT cwd,rollout_path FROM threads"):
                    if cwd:
                        add(cwd, "Chat working directory")
                    if rollout and not within(absolute(rollout), codex_home):
                        add(rollout, "External chat transcript")
            if "path" in tables.get("project_roots", []):
                for (p,) in con.execute("SELECT DISTINCT path FROM project_roots"):
                    add(p, "Registered project root")
            if {"attachment_type", "payload"} <= set(tables.get("thread_attachments", [])):
                for kind, payload in con.execute("SELECT attachment_type,payload FROM thread_attachments"):
                    if kind == "worktree":
                        obj = json.loads(payload)
                        for key in ("path", "workspaceDirectory", "workspace_directory", "root"):
                            if isinstance(obj.get(key), str) and obj[key].startswith("/"):
                                add(obj[key], "Attached worktree")
            databases.append(item)
    def collapsed():
        selected = []
        for p in sorted((Path(x) for x in roots), key=lambda x: (len(x.parts), str(x))):
            if not any(within(p, r) and r.is_dir() and not r.is_symlink() for r in selected):
                selected.append(p)
        return selected

    # A managed worktree can be nested under CODEX_HOME rather than a discovered root.
    scanned = set()
    while True:
        pending = [r for r in collapsed() if str(r) not in scanned]
        if not pending:
            break
        for root in pending:
            scanned.add(str(root))
            if root.is_symlink() or not root.is_dir():
                continue
            for directory, dirs, files in os.walk(root, followlinks=False):
                dirs[:] = [d for d in dirs if d != ".git"]
                if ".git" not in files:
                    continue
                gitfile = Path(directory) / ".git"
                if gitfile.is_symlink():
                    continue
                value = gitfile.read_text().strip()
                if value.startswith("gitdir: "):
                    admin = absolute(gitfile.parent / value[8:])
                    commonfile = admin / "commondir"
                    if commonfile.exists():
                        common = absolute(admin / commonfile.read_text().strip())
                        add(common.parent if common.name == ".git" else common,
                            "Git common repository for linked worktree")
                    elif not admin.exists():
                        missing.append({"path": str(admin), "reason": "Missing Git worktree administration"})
    selected = collapsed()
    if (home / ".lima").exists():
        dependencies.append("Lima VMs exist at " + str(home / ".lima") + "; VM disks/configuration require separate migration or explicit --include while the VM is stopped.")
    return {"format": FORMAT, "source_home": str(home), "codex_home": str(codex_home),
            "app_version": app_version(), "roots": [str(p) for p in selected],
            "discovered": {p: sorted(v) for p, v in roots.items()},
            "missing": missing, "warnings": warnings, "dependencies": dependencies,
            "databases": databases, "notes": NOTES}


def runtime_exclusion(p, root, codex_home, home):
    # Only omit ephemeral coordination objects in app state, never project lockfiles.
    if p.parent == codex_home and p.name in {"ipc", "thread-writer-locks", "project-metadata-locks", ".sqlite-maintenance.lock"}:
        return "Codex runtime coordination"
    app = home / "Library/Application Support/Codex"
    if p.parent == app and p.name in {"SingletonLock", "SingletonCookie", "SingletonSocket"}:
        return "Browser process lock"
    if p.name.endswith("-shm"):
        db = p.with_name(p.name[:-4])
        if db.is_file():
            with db.open("rb") as stream:
                if stream.read(16) == b"SQLite format 3\x00":
                    return "SQLite shared-memory index; regenerated from database and preserved WAL"
    return None


def xattrs(path):
    if not hasattr(os, "listxattr") or path.is_symlink():
        return {}
    return {name: base64.b64encode(os.getxattr(path, name, follow_symlinks=False)).decode("ascii")
            for name in os.listxattr(path, follow_symlinks=False)}


def walk_roots(report, excluded):
    home, ch = Path(report["source_home"]), Path(report["codex_home"])
    entries, omissions, external = [], [], []
    all_roots = [Path(p) for p in report["roots"]]
    for idx, root in enumerate(all_roots):
        def visit(p, arc):
            if any(within(p, e) for e in excluded):
                omissions.append({"path": str(p), "reason": "Backup output/staging location"})
                return
            reason = runtime_exclusion(p, root, ch, home)
            if reason:
                omissions.append({"path": str(p), "reason": reason})
                return
            st = p.lstat()
            entry = {"name": arc, "mode": stat.S_IMODE(st.st_mode), "mtime_ns": st.st_mtime_ns}
            if stat.S_ISLNK(st.st_mode):
                entry.update(type="symlink", target=os.readlink(p))
                target = p.resolve()
                if not any(within(target, r) for r in all_roots):
                    external.append({"link": str(p), "target": str(target), "exists": target.exists()})
            elif stat.S_ISDIR(st.st_mode):
                entry.update(type="dir", xattrs=xattrs(p))
            elif stat.S_ISREG(st.st_mode):
                if getattr(st, "st_flags", 0) & 0x40000000:  # macOS SF_DATALESS
                    fail("Cloud placeholder is not downloaded: " + str(p))
                entry.update(type="file", size=st.st_size, xattrs=xattrs(p))
            elif stat.S_ISSOCK(st.st_mode):
                omissions.append({"path": str(p), "reason": "Unix socket; cannot migrate a running process"})
                return
            else:
                fail("Unsupported special file: " + str(p))
            entry["source"] = str(p)
            entries.append(entry)
            if entry["type"] == "dir":
                for child in sorted(p.iterdir()):
                    visit(child, arc + "/" + child.name)
        visit(root, "payload/r%04d" % idx)
    return entries, omissions, external


def database_checks(folder):
    results = []
    for p in folder.rglob("*"):
        if p.is_file() and not p.is_symlink():
            with p.open("rb") as stream:
                header = stream.read(16)
            if header == b"SQLite format 3\x00":
                with closing(connect_ro(p)) as con:
                    check = [row[0] for row in con.execute("PRAGMA quick_check")]
                    if check != ["ok"]:
                        fail("Database integrity check failed: " + str(p))
                    counts = {}
                    for table in ("threads", "thread_turns", "thread_items", "projects"):
                        if table in schema(con):
                            counts[table] = con.execute('SELECT count(*) FROM "' + table + '"').fetchone()[0]
                results.append({"path": str(p.relative_to(folder)), "quick_check": "ok", "counts": counts})
    return results


def backup(args):
    require_stopped()
    home, ch, output = absolute(args.home), absolute(args.codex_home), absolute(args.output)
    suffix = ".tar.gz.age" if args.encrypt else ".tar.gz"
    if not output.name.endswith(suffix):
        fail("Use an output filename ending in " + suffix)
    if output.exists():
        fail("Output already exists: " + str(output))
    if args.encrypt and not shutil.which("age"):
        fail("Install age first, or omit --encrypt to create a private unencrypted .tar.gz.")
    if args.encrypt and not sys.stdin.isatty():
        fail("Encrypted backups must run in Terminal so age can prompt for the passphrase.")
    output.parent.mkdir(parents=True, exist_ok=True)
    report = inventory(home, ch, args.include)
    if report["missing"] or report["warnings"]:
        if not args.allow_incomplete:
            fail("Discovery found missing/broad paths. Run inventory and resolve them, or explicitly use --allow-incomplete. " + json.dumps({k: report[k] for k in ("missing", "warnings")}))
    # Output folder is outside included roots by default. Reject ancestors to avoid silent data loss.
    if any(within(Path(r), output.parent) or within(output.parent, Path(r)) for r in report["roots"]):
        fail("Choose a backup folder completely outside all source roots.")
    with tempfile.TemporaryDirectory(prefix="codex-migration-") as tmp:
        stage = Path(tmp)
        entries, omissions, external = walk_roots(report, [output, output.parent, stage])
        if external and not args.allow_incomplete:
            fail("External symlink dependencies found. Add their targets with --include, or explicitly use --allow-incomplete: " + json.dumps(external[:20]))
        total = sum(e.get("size", 0) for e in entries)
        same_volume = stage.stat().st_dev == output.parent.stat().st_dev
        if shutil.disk_usage(stage).free < total * (3.1 if same_volume else 2.1) + 64 * 1024 * 1024:
            fail("Insufficient temporary disk space for staging.")
        if shutil.disk_usage(output.parent).free < total * 1.1 + 64 * 1024 * 1024:
            fail("Insufficient destination disk space for an archive (conservative uncompressed estimate).")
        manifest = dict(report, created_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                        entries=[], omissions=omissions, external_symlinks=external,
                        completeness="incomplete" if report["missing"] or report["warnings"] or external else "local-files-captured")
        for i, entry in enumerate(entries):
            source = Path(entry["source"])
            target = stage / entry["name"]
            target.parent.mkdir(parents=True, exist_ok=True)
            before = source.lstat()
            if entry["type"] == "dir":
                target.mkdir(exist_ok=True)
            elif entry["type"] == "symlink":
                target.symlink_to(entry["target"])
            else:
                shutil.copyfile(source, target, follow_symlinks=False)
                entry["sha256"] = digest(target)
            after = source.lstat()
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                fail("Source changed during backup; stop other writers and retry: " + str(source))
            manifest["entries"].append({k: v for k, v in entry.items() if k != "source"})
            if i and i % 2000 == 0:
                print("Staged", i, "entries", flush=True)
        # Re-scan to catch concurrent additions/deletions and directory metadata changes.
        again, _, _ = walk_roots(report, [output, output.parent, stage])
        if [{k: v for k, v in e.items() if k != "sha256"} for e in entries] != again:
            fail("Source inventory changed during backup. Quit all writers and retry.")
        manifest["database_checks"] = database_checks(stage / "payload")
        require_stopped()
        shutil.copyfile(Path(__file__), stage / "migration.py")
        (stage / "RESTORE.md").write_text(RESTORE_GUIDE, encoding="utf-8")
        manifest["helper_sha256"] = {name: digest(stage / name) for name in ("migration.py", "RESTORE.md")}
        write_json(stage / "manifest.json", manifest)
        plain = stage / "archive.tar.gz"
        with tarfile.open(plain, "w:gz", dereference=False) as tf:
            for e in manifest["entries"]:
                info = tf.gettarinfo(str(stage / e["name"]), arcname=e["name"])
                info.mode, info.mtime = e["mode"], e["mtime_ns"] / 1e9
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                if e["type"] == "file":
                    with (stage / e["name"]).open("rb") as stream:
                        tf.addfile(info, stream)
                else:
                    tf.addfile(info)
            for name in ("manifest.json", "migration.py", "RESTORE.md"):
                tf.add(stage / name, arcname=name, recursive=False)
        verify_plain(plain)
        partial = output.with_name(output.name + ".partial-" + uuid.uuid4().hex)
        try:
            if args.encrypt:
                subprocess.run(["age", "-p", "-o", str(partial), str(plain)], check=True)
            else:
                shutil.copyfile(plain, partial)
            os.chmod(partial, 0o600)
            os.replace(partial, output)
        finally:
            if partial.exists():
                partial.unlink()
        checksum = digest(output)
        output.with_name(output.name + ".sha256").write_text(checksum + "  " + output.name + "\n")
        summary = {"archive": str(output), "bytes": output.stat().st_size, "sha256": checksum,
                   "entries": len(entries), "completeness": manifest["completeness"],
                   "missing": report["missing"], "external_symlinks": external, "omissions": omissions}
        write_json(output.with_name(output.name + ".report.json"), summary)
        print(json.dumps(summary, indent=2))


class Archive:
    def __init__(self, path):
        self.path = absolute(path)
        self.tmp = None

    def __enter__(self):
        if self.path.suffix == ".age":
            if not shutil.which("age") or not sys.stdin.isatty():
                fail("Decrypt with age in Terminal, or install age and run this command interactively.")
            self.tmp = tempfile.TemporaryDirectory(prefix="codex-decrypt-")
            dest = Path(self.tmp.name) / "archive.tar.gz"
            try:
                subprocess.run(["age", "-d", "-o", str(dest), str(self.path)], check=True)
            except BaseException:
                self.tmp.cleanup()
                raise
            self.path = dest
        return self.path

    def __exit__(self, *exc):
        if self.tmp:
            self.tmp.cleanup()


def safe_name(name):
    p = PurePosixPath(name)
    return bool(name) and not p.is_absolute() and ".." not in p.parts and str(p) == name and "\\" not in name


def verify_plain(path):
    with tarfile.open(path, "r:gz") as tf:
        members = {}
        for member in tf:
            if not safe_name(member.name) or member.name in members:
                fail("Unsafe or duplicate archive path: " + member.name)
            if not (member.isdir() or member.isfile() or member.issym()):
                fail("Unsupported archive member type: " + member.name)
            members[member.name] = member
        if "manifest.json" not in members or not members["manifest.json"].isfile() or members["manifest.json"].size > 256 * 1024 * 1024:
            fail("Missing or oversized manifest.")
        manifest = json.load(tf.extractfile(members["manifest.json"]))
        if manifest.get("format") != FORMAT:
            fail("Unsupported backup format.")
        for name in ("migration.py", "RESTORE.md"):
            if name not in members or not members[name].isfile():
                fail("Missing or invalid helper: " + name)
            if hashlib.sha256(tf.extractfile(members[name]).read()).hexdigest() != manifest["helper_sha256"][name]:
                fail("Helper checksum mismatch: " + name)
        roots = manifest.get("roots", [])
        if not roots or any(not Path(r).is_absolute() or len(Path(r).parts) < 3 for r in roots):
            fail("Invalid manifest roots.")
        expected = {"manifest.json", "migration.py", "RESTORE.md"}
        kinds = {}
        for e in manifest["entries"]:
            name = e["name"]
            if not safe_name(name) or not re.match(r"^payload/r[0-9]{4}(?:/|$)", name) or name in expected:
                fail("Invalid manifest entry: " + name)
            idx = int(name.split("/")[1][1:])
            if idx >= len(roots):
                fail("Entry references nonexistent root: " + name)
            expected.add(name)
            kinds[name] = e["type"]
            member = members.get(name)
            if member is None:
                fail("Missing archive entry: " + name)
            if e["type"] == "file":
                if not member.isfile() or member.size != e["size"]:
                    fail("File type/size mismatch: " + name)
                sha = hashlib.sha256()
                stream = tf.extractfile(member)
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    sha.update(chunk)
                if sha.hexdigest() != e["sha256"]:
                    fail("Checksum mismatch: " + name)
            elif e["type"] == "symlink":
                if not member.issym() or member.linkname != e["target"]:
                    fail("Symlink mismatch: " + name)
            elif e["type"] != "dir" or not member.isdir():
                fail("Directory/type mismatch: " + name)
        for name in kinds:
            for parent in PurePosixPath(name).parents:
                if str(parent) in kinds and kinds[str(parent)] != "dir":
                    fail("Archive writes through a non-directory: " + name)
        if expected != set(members):
            fail("Archive has unexpected or missing members.")
        for i in range(len(roots)):
            if "payload/r%04d" % i not in kinds:
                fail("Missing payload root.")
        return manifest


def mappings(values):
    result = []
    for value in values:
        old, sep, new = value.partition("=")
        if not sep or not old.startswith("/") or not new.startswith("/"):
            fail("Mappings must be /absolute/old=/absolute/new")
        result.append((str(absolute(old)), str(absolute(new))))
    return sorted(result, key=lambda item: len(item[0]), reverse=True)


def mapped(value, pairs):
    if not isinstance(value, str):
        return value
    for old, new in pairs:
        if value == old or value.startswith(old + "/"):
            return new + value[len(old):]
    return value


def map_object(value, pairs):
    if isinstance(value, dict):
        return {mapped(k, pairs): map_object(v, pairs) for k, v in value.items()}
    if isinstance(value, list):
        return [map_object(v, pairs) for v in value]
    return mapped(value, pairs)


def remap(stage, manifest, pairs):
    """Known operational fields only. Never bulk replace transcript content."""
    changes = []
    ch = Path(manifest["codex_home"])
    for idx, root in enumerate(manifest["roots"]):
        rp = Path(root)
        if within(ch, rp):
            local = stage / ("payload/r%04d" % idx) / ch.relative_to(rp)
            for g in (local / ".codex-global-state.json", local / ".codex-global-state.json.bak"):
                if g.exists():
                    write_json(g, map_object(read_json(g), pairs))
                    changes.append(str(g.relative_to(stage)))
            for db in local.glob("state_*.sqlite"):
                with closing(sqlite3.connect(db)) as con:
                    tables = schema(con)
                    if not {"cwd", "rollout_path"} <= set(tables.get("threads", [])):
                        fail("Unrecognized state database schema for path remapping: " + db.name)
                    for table, columns in {"threads": ("cwd", "rollout_path"), "project_roots": ("path",)}.items():
                        for col in columns:
                            if col not in tables.get(table, []):
                                continue
                            for rowid, val in list(con.execute('SELECT rowid,"%s" FROM "%s"' % (col, table))):
                                new = mapped(val, pairs)
                                if new != val:
                                    con.execute('UPDATE "%s" SET "%s"=? WHERE rowid=?' % (table, col), (new, rowid))
                    if "payload" in tables.get("thread_attachments", []):
                        for rowid, key, payload in list(con.execute("SELECT rowid,identity_key,payload FROM thread_attachments")):
                            con.execute("UPDATE thread_attachments SET identity_key=?,payload=? WHERE rowid=?",
                                        (mapped(key, pairs), json.dumps(map_object(json.loads(payload), pairs)), rowid))
                    for table, col in (("projects", "metadata"), ("threads", "sandbox_policy")):
                        if col in tables.get(table, []):
                            for rowid, value in list(con.execute('SELECT rowid,"%s" FROM "%s"' % (col, table))):
                                try:
                                    new = json.dumps(map_object(json.loads(value), pairs))
                                except (ValueError, TypeError):
                                    continue
                                con.execute('UPDATE "%s" SET "%s"=? WHERE rowid=?' % (table, col), (new, rowid))
                    con.commit()
                    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                changes.append(str(db.relative_to(stage)))
                # A successful checkpoint can remove a captured WAL; retain an empty WAL
                # entry for the metadata pass. The committed main DB now contains its data.
                wal = db.with_name(db.name + "-wal")
                if any(e["name"] == str(wal.relative_to(stage)) for e in manifest["entries"]):
                    if not wal.exists():
                        wal.touch()
                    changes.append(str(wal.relative_to(stage)))
            for dirname in ("sessions", "archived_sessions"):
                for p in (local / dirname).rglob("*.jsonl") if (local / dirname).exists() else []:
                    # Only the leading session_meta event contains operational cwd.
                    with p.open("rb") as stream:
                        first = stream.readline()
                    try:
                        event = json.loads(first)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if event.get("type") == "session_meta" and isinstance(event.get("payload"), dict):
                        payload = event["payload"]
                        if "cwd" in payload:
                            payload["cwd"] = mapped(payload["cwd"], pairs)
                            replacement = p.with_name(p.name + ".migration-new")
                            with p.open("rb") as source, replacement.open("wb") as target:
                                source.readline()
                                target.write(json.dumps(event, ensure_ascii=False).encode() + b"\n")
                                shutil.copyfileobj(source, target, 1024 * 1024)
                            os.replace(replacement, p)
                            changes.append(str(p.relative_to(stage)))
            for p in (local / "automations").glob("*/automation.toml"):
                text = p.read_text()
                def replace_cwds(match):
                    # cwds uses TOML/JSON-compatible string arrays in known automation files.
                    try:
                        values = json.loads(match.group(1))
                    except ValueError:
                        fail("Automation cwds needs manual path update: " + str(p))
                    return "cwds = " + json.dumps([mapped(v, pairs) for v in values])
                p.write_text(re.sub(r"(?m)^cwds\s*=\s*(\[[^\n]*\])", replace_cwds, text))
                changes.append(str(p.relative_to(stage)))
    for e in manifest["entries"]:
        p = stage / e["name"]
        if e["type"] == "symlink":
            new = mapped(e["target"], pairs)
            if new != e["target"]:
                p.unlink()
                p.symlink_to(new)
                changes.append(e["name"])
        if e["type"] == "file" and (p.name == ".git" or p.name in {"gitdir", "commondir"} and ".git" in p.parts):
            text = p.read_text()
            prefix = "gitdir: " if text.startswith("gitdir: ") else ""
            new = prefix + mapped(text[len(prefix):].strip(), pairs) + "\n"
            if new != text:
                p.write_text(new)
                changes.append(e["name"])
    return changes


def no_symlink_parents(path):
    for p in path.parents:
        if p.is_symlink():
            fail("Destination has a symlink ancestor; choose a real destination: " + str(p))


def sync_preferences(destinations):
    warnings = []
    for p in destinations:
        if (p.parent == Path.home() / "Library/Preferences" and p.is_file()
                and p.name in {"com.openai.codex.plist", "com.openai.codex.installer.plist"}):
            try:
                subprocess.run(["defaults", "import", p.stem, str(p)], check=True, capture_output=True)
            except (OSError, subprocess.CalledProcessError):
                warnings.append("Preference cache needs manual sync in Terminal: defaults import %s %s" % (p.stem, str(p)))
    return warnings


def restore(args):
    pairs = mappings(args.map)
    with Archive(args.archive) as archive:
        manifest = verify_plain(archive)
        actual_home = str(Path.home())
        if manifest["source_home"] != actual_home and mapped(manifest["source_home"], pairs) != actual_home:
            fail("Different macOS home directory. Supply --map %s=%s" % (manifest["source_home"], actual_home))
        destinations = [absolute(mapped(p, pairs)) for p in manifest["roots"]]
        for d in destinations:
            if d == Path.home() or len(d.parts) < 3:
                fail("Unsafe restore root: " + str(d))
            no_symlink_parents(d)
        for i, a in enumerate(destinations):
            if any(within(a, b) or within(b, a) for b in destinations[:i]):
                fail("Restore destinations overlap after mapping.")
        current = app_version()
        mismatch = current and manifest.get("app_version") and current != manifest["app_version"]
        preview = {"dry_run": not args.apply, "source_app": manifest.get("app_version"), "installed_app": current,
                   "version_mismatch": bool(mismatch), "completeness": manifest["completeness"],
                   "destinations": [{"path": str(p), "existing_will_be_moved_aside": p.exists() or p.is_symlink()} for p in destinations],
                   "mappings": pairs, "notes": manifest["notes"], "external_symlinks": manifest["external_symlinks"]}
        print(json.dumps(preview, indent=2), flush=True)
        if not args.apply:
            return
        require_stopped()
        if mismatch and not args.accept_version_mismatch:
            fail("Install the original Codex version, or inspect compatibility and explicitly use --accept-version-mismatch.")
        if any(within(absolute(args.archive), d) for d in destinations):
            fail("Move the archive outside the restore destinations first.")
        txid = dt.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
        journal_path = Path.home() / ".codex-migration-transactions" / (txid + ".json")
        if any(within(journal_path, d) for d in destinations):
            fail("Transaction journal overlaps a restore destination.")
        journal = {"format": FORMAT, "id": txid, "status": "preparing", "operations": [], "created_parents": []}
        write_json(journal_path, journal)
        staged = []
        try:
            for i, dest in enumerate(destinations):
                missing = []
                parent = dest.parent
                while not parent.exists():
                    missing.append(parent)
                    parent = parent.parent
                for p in reversed(missing):
                    p.mkdir()
                    journal["created_parents"].append(str(p))
                need = sum(e.get("size", 0) for e in manifest["entries"] if e["name"].split("/")[1] == "r%04d" % i)
                if shutil.disk_usage(dest.parent).free < need * 1.1 + 32 * 1024 * 1024:
                    fail("Insufficient space on destination volume: " + str(dest.parent))
                temp = Path(tempfile.mkdtemp(prefix=".codex-migration-stage-", dir=dest.parent))
                os.chmod(temp, 0o700)
                staged.append(temp)
                write_json(journal_path, journal)
            # Extract files into separate containers on each destination volume. Symlinks last.
            with tarfile.open(archive, "r:gz") as tf:
                for e in sorted(manifest["entries"], key=lambda x: x["type"] == "symlink"):
                    parts = PurePosixPath(e["name"]).parts
                    idx = int(parts[1][1:])
                    target = staged[idx] / "root"
                    for part in parts[2:]:
                        target = target / part
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if e["type"] == "dir":
                        target.mkdir(exist_ok=True)
                    elif e["type"] == "symlink":
                        target.symlink_to(e["target"])
                    else:
                        with tf.extractfile(e["name"]) as source, target.open("xb") as out:
                            shutil.copyfileobj(source, out, 1024 * 1024)
                        if digest(target) != e["sha256"]:
                            fail("Extracted checksum mismatch: " + e["name"])
            # Rebuild a virtual payload tree of directory links for remapping/checks.
            with tempfile.TemporaryDirectory(prefix="codex-check-") as tmp:
                view = Path(tmp)
                (view / "payload").mkdir()
                for i, p in enumerate(staged):
                    (view / "payload" / ("r%04d" % i)).symlink_to(p / "root")
                changes = remap(view, manifest, pairs) if pairs else []
                checks = []
                for p in staged:
                    if (p / "root").is_dir() and not (p / "root").is_symlink():
                        checks.extend(database_checks(p / "root"))
                journal["database_checks"] = checks
                journal["remapped_files"] = changes
            # Apply file metadata after remapping, directories last so children don't alter mtimes.
            for e in sorted(manifest["entries"], key=lambda x: (x["type"] == "dir", -len(PurePosixPath(x["name"]).parts))):
                parts = PurePosixPath(e["name"]).parts
                p = staged[int(parts[1][1:])] / "root" / Path(*parts[2:])
                if e["type"] != "symlink":
                    os.chmod(p, e["mode"])
                    for key, val in e.get("xattrs", {}).items():
                        os.setxattr(p, key, base64.b64decode(val), follow_symlinks=False)
                    os.utime(p, ns=(e["mtime_ns"], e["mtime_ns"]))
            for e in manifest["entries"]:
                if e["type"] == "file" and e["name"] not in changes:
                    parts = PurePosixPath(e["name"]).parts
                    p = staged[int(parts[1][1:])] / "root" / Path(*parts[2:])
                    if digest(p) != e["sha256"]:
                        fail("File changed unexpectedly during restore: " + e["name"])
            # Commit roots via same-volume renames; journal each phase for recovery.
            journal["status"] = "committing"
            write_json(journal_path, journal)
            for dest, temp in zip(destinations, staged):
                op = {"destination": str(dest), "backup": str(dest.with_name("." + dest.name + ".codex-rollback-" + txid)),
                      "had_original": dest.exists() or dest.is_symlink(), "phase": "pending"}
                journal["operations"].append(op)
                write_json(journal_path, journal)
                if op["had_original"]:
                    os.replace(dest, op["backup"])
                op["phase"] = "original_saved"
                write_json(journal_path, journal)
                os.replace(temp / "root", dest)
                op["phase"] = "installed"
                write_json(journal_path, journal)
            journal["status"] = "complete"
            journal["warnings"] = sync_preferences(destinations)
            write_json(journal_path, journal)
            print("Restored. Rollback journal: " + str(journal_path))
            for warning in journal["warnings"]:
                print("WARNING: " + warning)
            if pairs:
                print("Paths remapped in %d known files. Run scan-references with the old path before launching Codex." % len(journal["remapped_files"]))
        except BaseException:
            journal["status"] = "interrupted"
            write_json(journal_path, journal)
            print("Restore interrupted. Keep this journal for rollback: " + str(journal_path), file=sys.stderr)
            raise
        finally:
            for p in staged:
                shutil.rmtree(p, ignore_errors=True)


def rollback(args):
    path = absolute(args.journal)
    journal = read_json(path)
    print(json.dumps({"dry_run": not args.apply, "operations": journal["operations"]}, indent=2))
    if not args.apply:
        return
    require_stopped()
    for op in reversed(journal["operations"]):
        dest, saved = Path(op["destination"]), Path(op["backup"])
        no_symlink_parents(dest)
        if op.get("rolled_back"):
            continue
        if saved.exists() or saved.is_symlink():
            if dest.exists() or dest.is_symlink():
                moved = dest.with_name(dest.name + ".after-migration-" + journal["id"])
                if moved.exists() or moved.is_symlink():
                    fail("Rollback conflict; preserve and move aside: " + str(moved))
                os.replace(dest, moved)
            os.replace(saved, dest)
        elif op["phase"] in {"original_saved", "installed"} and not op["had_original"] and (dest.exists() or dest.is_symlink()):
            moved = dest.with_name(dest.name + ".after-migration-" + journal["id"])
            if moved.exists() or moved.is_symlink():
                fail("Rollback conflict: " + str(moved))
            os.replace(dest, moved)
        elif op["had_original"] and op["phase"] != "pending":
            fail("Original rollback copy is missing: " + str(saved))
        op["rolled_back"] = True
        write_json(path, journal)
    journal["status"] = "rolled_back"
    journal["warnings"] = sync_preferences([Path(op["destination"]) for op in journal["operations"]])
    write_json(path, journal)
    print("Rollback complete. Restored migration data was moved aside, not deleted.")


def scan_references(args):
    old = args.old.encode()
    found = []
    for root in args.path:
        p = absolute(root)
        candidates = p.rglob("*") if p.is_dir() else [p]
        for f in candidates:
            if f.is_file() and not f.is_symlink():
                # Chunked literal search; report paths only, never message/secret values.
                with f.open("rb") as stream:
                    carry = b""
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        if old in carry + chunk:
                            found.append(str(f))
                            break
                        carry = chunk[-max(len(old) - 1, 0):] if len(old) > 1 else b""
    print(json.dumps({"old_prefix": args.old, "files_containing_reference": found,
                      "note": "Includes historical messages and binary caches. Inspect operational references; never bulk replace chat text."}, indent=2))


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    for command in ("inventory", "backup"):
        sp = sub.add_parser(command)
        sp.add_argument("--home", default=str(Path.home()))
        sp.add_argument("--codex-home", default=os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        sp.add_argument("--include", action="append", default=[], help="Additional dependency/project root; repeatable")
        sp.add_argument("--output", required=True)
        if command == "inventory":
            sp.add_argument("--deep", action="store_true", help="Read file metadata to estimate size and find external symlinks")
        if command == "backup":
            sp.add_argument("--allow-incomplete", action="store_true", help="Explicitly accept reported missing paths or external symlink dependencies")
            sp.add_argument("--encrypt", action="store_true", help="Use age passphrase encryption; run in Terminal")
    sp = sub.add_parser("verify")
    sp.add_argument("archive")
    sp = sub.add_parser("restore")
    sp.add_argument("archive")
    sp.add_argument("--map", action="append", default=[])
    sp.add_argument("--apply", action="store_true")
    sp.add_argument("--accept-version-mismatch", action="store_true")
    sp = sub.add_parser("rollback")
    sp.add_argument("journal")
    sp.add_argument("--apply", action="store_true")
    sp = sub.add_parser("scan-references")
    sp.add_argument("--old", required=True)
    sp.add_argument("path", nargs="+")
    return p


def main():
    args = parser().parse_args()
    if args.command == "inventory":
        report = inventory(absolute(args.home), absolute(args.codex_home), args.include)
        if args.deep:
            try:
                entries, omissions, external = walk_roots(report, [absolute(args.output)])
                report.update(entry_count=len(entries), uncompressed_bytes=sum(e.get("size", 0) for e in entries),
                              omissions=omissions, external_symlinks=external, deep_scan_complete=True)
            except (OSError, RuntimeError) as exc:
                report.update(deep_scan_complete=False, deep_scan_error=str(exc))
        write_json(absolute(args.output), report)
        print(json.dumps({"report": str(absolute(args.output)), "roots": len(report["roots"]),
                          "missing": len(report["missing"]), "warnings": report["warnings"],
                          "deep_scan_error": report.get("deep_scan_error")}, indent=2))
        if args.deep and not report["deep_scan_complete"]:
            sys.exit(2)
    elif args.command == "backup":
        backup(args)
    elif args.command == "verify":
        with Archive(args.archive) as path:
            manifest = verify_plain(path)
        print(json.dumps({"verified": True, "entries": len(manifest["entries"]),
                          "created_at": manifest["created_at"], "completeness": manifest["completeness"]}, indent=2))
    elif args.command == "restore":
        restore(args)
    elif args.command == "rollback":
        rollback(args)
    else:
        scan_references(args)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError, TypeError, EOFError, sqlite3.Error, tarfile.TarError, subprocess.CalledProcessError) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        sys.exit(1)
