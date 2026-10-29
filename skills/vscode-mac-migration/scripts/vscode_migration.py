#!/usr/bin/env python3
"""Back up VS Code (settings, extensions list, Copilot chat history, workspace state) and the
projects it knows about, then restore on a new Mac. Python 3.9+, standard library only;
encryption uses the macOS openssl command.

Projects in a cloud-synced folder, or in a git repository whose work is all committed and
pushed, are linked instead of copied: restore clones them back (and puts git-ignored local
files like .env back) or waits for the sync service. Per-project data such as Copilot chats is
re-filed under the ID VS Code computes for the project on the new Mac.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import uuid

FORMAT = 1
TOOL = "vscode-mac-migration"
# (Application Support folder, dot folder in home, app bundle)
FLAVORS = (("Code", ".vscode", "Visual Studio Code.app"),
           ("Code - Insiders", ".vscode-insiders", "Visual Studio Code - Insiders.app"))
APP_PARTS = ("User", "Backups", "Workspaces", "agentSessionData")  # under Application Support/<flavor>
DOT_PARTS = ("argv.json", "agent-plugins")                          # under ~/.vscode (extensions are reinstalled)
EXTRA_HOME = ((".copilot", {"logs"}),)                              # GitHub Copilot CLI chats and plugins
CLOUD_PARENTS = ("Library/CloudStorage", "Library/Mobile Documents")
REBUILDABLE = {"node_modules", ".venv", "venv", "env", "__pycache__", ".pytest_cache", ".mypy_cache",
               ".ruff_cache", ".tox", ".nox", "dist", "build", ".next", ".nuxt", ".svelte-kit", ".turbo",
               ".parcel-cache", ".cache", "target", ".gradle", "DerivedData", "Pods", "coverage",
               ".DS_Store", ".eggs", "bower_components", ".terraform"}
# Skipped inside copied projects: only folders that are always safe to rebuild.
COPY_SKIP = {"node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
             ".tox", ".nox", ".next", ".nuxt", ".svelte-kit", ".turbo", ".parcel-cache", "DerivedData",
             ".DS_Store", "bower_components"}
HISTORY_DIRS = {"chatSessions", "chatEditingSessions", "emptyWindowChatSessions"}  # never rewritten
SF_DATALESS = 0x40000000
RESTORE_GUIDE = """# Restore a VS Code migration backup

1. Install VS Code on the new Mac, open it once, then quit it (Cmd+Q).
2. In Terminal, extract the helper and preview:

       tar -xzf vscode-backup.tar.gz vscode_migration.py      # unencrypted archive
       python3 vscode_migration.py restore /path/to/vscode-backup.tar.gz[.enc]

   For an .enc archive, use the copy of vscode_migration.py saved next to it.
3. If the preview looks right, add --apply. Use --map /Users/old=/Users/new only for
   folders that moved; a different home folder is mapped automatically.
4. Linked git projects are cloned (needs access, e.g. `gh auth login` or an SSH key).
   Cloud folders arrive when you sign in to OneDrive/iCloud/Dropbox. Afterwards run
   `relink <archive> --apply` to clone anything skipped and re-file chat history.
5. Open VS Code and sign in to GitHub/Copilot. If you use Settings Sync, turn it on
   only after restoring and choose to keep the local data.
"""


# ---------------------------------------------------------------- helpers

def fail(message):
    raise RuntimeError(message)


def absolute(value):
    return Path(os.path.abspath(os.path.expanduser(str(value))))


def within(path, parent):
    path, parent = Path(path), Path(parent)
    return path == parent or parent in path.parents


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".new")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_sqlite(path):
    try:
        with open(path, "rb") as stream:
            return stream.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def uri_to_path(uri):
    parsed = urllib.parse.urlparse(uri)
    return urllib.parse.unquote(parsed.path) if parsed.scheme == "file" else None


def birth_ms(path):
    # Matches Node's fs.Stat.birthtime.getTime() on macOS.
    return int(round(os.stat(path).st_birthtime * 1e6)) // 1000


def folder_id(path):
    """ID VS Code uses for a single-folder workspace on macOS: md5(path + birthtime in ms)."""
    return hashlib.md5((str(path) + str(birth_ms(path))).encode()).hexdigest()


def workspace_file_id(path):
    """ID VS Code uses for a .code-workspace file on macOS: md5(lowercased path)."""
    return hashlib.md5(str(path).lower().encode()).hexdigest()


def cloud_provider(path, home):
    real = Path(os.path.realpath(path))
    for base in CLOUD_PARENTS:
        parent = Path(os.path.realpath(home / base))
        if within(real, parent) and real != parent:
            name = real.relative_to(parent).parts[0]
            if base.endswith("Mobile Documents"):
                return "iCloud Drive" if name == "com~apple~CloudDocs" else "iCloud (" + name + ")"
            return name
    return None


def git_out(repo, *args):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=120, env=env)
    if p.returncode:
        raise RuntimeError(p.stderr.strip() or "git failed")
    return p.stdout


def git_state(root):
    """Read-only description of the git repository containing root, or None. "Pushed" uses the last fetch."""
    if not shutil.which("git") or not root.is_dir() or root.is_symlink():
        return None
    try:
        top_real = Path(git_out(root, "rev-parse", "--show-toplevel").strip())
        rel = Path(os.path.realpath(root)).relative_to(top_real)
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired):
        return None
    top = root
    for _ in rel.parts:
        top = top.parent
    state = {"toplevel": str(top), "issues": [], "remotes": {}, "ignored": []}
    try:
        git_dir = Path(os.path.realpath(top / git_out(top, "rev-parse", "--git-dir").strip()))
        common = Path(os.path.realpath(top / git_out(top, "rev-parse", "--git-common-dir").strip()))
        for line in git_out(top, "remote", "-v").splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[2] == "(fetch)":
                state["remotes"][parts[0]] = parts[1]
        try:
            state["head"] = git_out(top, "rev-parse", "HEAD").strip()
        except RuntimeError:
            state["issues"].append("Repository has no commits yet")
            return state
        state["branch"] = git_out(top, "rev-parse", "--abbrev-ref", "HEAD").strip()
        changes = [x for x in git_out(top, "status", "--porcelain=v1", "--untracked-files=normal").splitlines() if x]
        stashes = [x for x in git_out(top, "stash", "list").splitlines() if x]
        if git_dir != common:
            state["issues"].append("Is a linked git worktree; copied as files")
        if not state["remotes"]:
            state["issues"].append("No remote (e.g. GitHub) configured; push it somewhere to avoid copying it")
        if changes:
            state["issues"].append("%d uncommitted or untracked change(s)" % len(changes))
            state["changes"] = changes[:50]
        if state["remotes"]:
            unpushed = int(git_out(top, "rev-list", "--count", "--branches", "HEAD", "--not", "--remotes").strip() or 0)
            if unpushed:
                state["issues"].append("%d commit(s) not pushed" % unpushed)
                state["unpushed_branches"] = [
                    b for b in git_out(top, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()
                    if int(git_out(top, "rev-list", "--count", b, "--not", "--remotes").strip() or 0)]
        if stashes:
            state["issues"].append("%d stash(es), which exist only on this Mac" % len(stashes))
        if not state["issues"]:
            out = git_out(top, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z")
            for item in out.split("\0"):
                item = item.rstrip("/")
                if item and not any(p in REBUILDABLE for p in Path(item).parts) and not item.endswith(".pyc"):
                    state["ignored"].append(item)
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        state["issues"].append("Could not read git state: " + str(exc))
    return state


def running_vscode():
    try:
        out = subprocess.run(["ps", "-axo", "command="], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        fail("Cannot check running processes. Run this in macOS Terminal after quitting VS Code.")
    return [line for line in out.splitlines()
            if any("/" + app + "/Contents/" in line for _, _, app in FLAVORS)]


def require_closed():
    if running_vscode():
        fail("Quit VS Code first (Cmd+Q), including other windows, then run this again.")


def code_cli(app):
    for candidate in (Path("/Applications") / app / "Contents/Resources/app/bin/code",
                      Path.home() / "Applications" / app / "Contents/Resources/app/bin/code"):
        if candidate.exists():
            return str(candidate)
    return shutil.which("code") if "Insiders" not in app else shutil.which("code-insiders")


def app_version(app):
    import plistlib
    try:
        info = plistlib.loads((Path("/Applications") / app / "Contents/Info.plist").read_bytes())
        return info.get("CFBundleShortVersionString")
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- inventory

def installed_extensions(dot):
    data = dot / "extensions" / "extensions.json"
    result = []
    if data.exists():
        try:
            for item in json.loads(data.read_text(encoding="utf-8")):
                ident = item.get("identifier", {}).get("id")
                if ident:
                    result.append(ident + ("@" + item["version"] if item.get("version") else ""))
        except (ValueError, AttributeError):
            pass
    if not result and (dot / "extensions").is_dir():
        for d in sorted((dot / "extensions").iterdir()):
            m = re.match(r"^(.+?\..+?)-(\d[\w.\-]*)$", d.name)
            if d.is_dir() and m:
                result.append(m.group(1) + "@" + m.group(2))
    return sorted(set(result))


def workspace_targets(storage_dir):
    """Yield (storage folder, kind, value) for each workspace.json VS Code keeps."""
    if not storage_dir.is_dir():
        return
    for d in sorted(storage_dir.iterdir()):
        wj = d / "workspace.json"
        if not wj.is_file():
            continue
        try:
            data = json.loads(wj.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if "folder" in data:
            yield d, "folder", data["folder"]
        elif "workspace" in data:
            yield d, "workspace", data["workspace"]


def workspace_file_folders(path):
    try:
        data = json.loads(re.sub(r"(?m)^\s*//.*$", "", Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return []
    folders = []
    for f in data.get("folders", []):
        if "path" in f:
            folders.append(absolute(Path(path).parent / f["path"]))
        elif "uri" in f and uri_to_path(f["uri"]):
            folders.append(absolute(uri_to_path(f["uri"])))
    return folders


def inventory(home, includes=()):
    home = absolute(home)
    flavors, roots, projects, remote, missing, warnings = [], [], {}, [], [], []
    for name, dot, app in FLAVORS:
        support = home / "Library/Application Support" / name
        if not support.exists() and not (home / dot).exists():
            continue
        flavors.append({"name": name, "dot": dot, "app": app, "version": app_version(app),
                        "extensions": installed_extensions(home / dot)})
        for part in APP_PARTS:
            if (support / part).exists():
                roots.append({"path": str(support / part), "kind": "app", "skip": []})
        for part in DOT_PARTS:
            if (home / dot / part).exists():
                roots.append({"path": str(home / dot / part), "kind": "app", "skip": []})
        for storage, kind, value in workspace_targets(support / "User/workspaceStorage"):
            p = uri_to_path(value)
            if p is None:
                remote.append(value)
                continue
            paths = [absolute(p)]
            if kind == "workspace" and paths[0].exists():
                paths += workspace_file_folders(paths[0])
                if within(paths[0], support):
                    paths = paths[1:]  # untitled workspaces live in app data and are copied with it
            for path in paths:
                if path.exists():
                    projects.setdefault(str(path), set()).add("Opened in " + name)
                elif str(path) not in [m["path"] for m in missing]:
                    missing.append({"path": str(path), "storage": storage.name,
                                    "note": "Folder no longer exists here; its VS Code data is kept but stays orphaned"})
    for rel, skip in EXTRA_HOME:
        if (home / rel).exists():
            roots.append({"path": str(home / rel), "kind": "app", "skip": sorted(skip)})
    for value in includes:
        p = absolute(value)
        if not p.exists():
            fail("--include path does not exist: " + str(p))
        projects.setdefault(str(p), set()).add("Explicit include")
    if not flavors:
        fail("No VS Code data found in " + str(home))

    # Drop projects nested in other projects; refuse overly broad folders.
    selected = []
    for p in sorted((Path(x) for x in projects), key=lambda x: (len(x.parts), str(x))):
        if p == home or len(p.parts) < 3:
            warnings.append("Skipped a project that is your whole home or a top-level folder: " + str(p))
        elif not any(within(p, s) for s in selected):
            selected.append(p)

    linked, attention, seen = [], [], set()
    explicit = {str(absolute(v)) for v in includes}
    for p in selected:
        provider = cloud_provider(p, home)
        if provider and str(p) not in explicit:
            linked.append({"kind": "cloud", "path": str(p), "provider": provider})
            continue
        state = None if str(p) in explicit else git_state(p)
        if state and not state["issues"]:
            top = Path(state["toplevel"])
            if str(top) in seen:
                continue
            seen.add(str(top))
            linked.append({"kind": "git", "path": str(top), "remotes": state["remotes"], "branch": state["branch"],
                           "head": state["head"], "local_only_files": state["ignored"]})
            for item in state["ignored"]:
                roots.append({"path": str(top / item), "kind": "local-only", "skip": sorted(REBUILDABLE)})
            continue
        if state:
            attention.append({"path": str(p), **{k: v for k, v in state.items() if k != "ignored"}})
        roots.append({"path": str(p), "kind": "project", "skip": sorted(COPY_SKIP)})

    return {"format": FORMAT, "tool": TOOL, "source_home": str(home), "flavors": flavors, "roots": roots,
            "linked": linked, "attention": attention, "remote_workspaces": remote,
            "missing_projects": missing, "warnings": warnings}


def walk(root):
    """Yield (path, lstat) for everything under a root, skipping rebuildable/ignored names."""
    p = Path(root["path"])
    skip = set(root["skip"])
    st = p.lstat()
    yield p, st
    if p.is_dir() and not p.is_symlink():
        for directory, dirs, files in os.walk(p, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in skip)
            for name in sorted(dirs) + sorted(f for f in files if f not in skip):
                child = Path(directory) / name
                yield child, child.lstat()


def measure(report):
    total = 0
    for root in report["roots"]:
        for p, st in walk(root):
            if getattr(st, "st_flags", 0) & SF_DATALESS:
                fail("Cloud placeholder is not downloaded (download it or exclude the folder): " + str(p))
            if os.path.isfile(p) and not os.path.islink(p):
                total += st.st_size
    return total


# ---------------------------------------------------------------- backup

def stage_name(path):
    return "files/" + str(path).lstrip("/")


def copy_sqlite(src, dst):
    with sqlite3.connect(Path(src).as_uri() + "?mode=ro", uri=True) as source, sqlite3.connect(dst) as target:
        source.backup(target)
    source.close()
    target.close()


def password_args(env_name):
    if os.environ.get(env_name):
        return ["-pass", "env:" + env_name]
    if not sys.stdin.isatty():
        fail("Run this in Terminal so openssl can ask for the password (or set %s)." % env_name)
    return []


def backup(args):
    if not args.force:
        require_closed()
    home, output = absolute(args.home), absolute(args.output)
    suffix = ".tar.gz.enc" if args.encrypt else ".tar.gz"
    if not output.name.endswith(suffix):
        fail("Use an output filename ending in " + suffix)
    if output.exists():
        fail("Output already exists: " + str(output))
    report = inventory(home, args.include)
    if any(within(output, r["path"]) for r in report["roots"]):
        fail("Choose an output folder outside the folders being backed up.")
    output.parent.mkdir(parents=True, exist_ok=True)
    files, symlinks = {}, {}
    with tempfile.TemporaryDirectory(prefix="vscode-migration-") as tmp:
        stage = Path(tmp) / "stage"
        for root in report["roots"]:
            for p, st in walk(root):
                if getattr(st, "st_flags", 0) & SF_DATALESS:
                    fail("Cloud placeholder is not downloaded (download it or exclude the folder): " + str(p))
                target = stage / stage_name(p)
                target.parent.mkdir(parents=True, exist_ok=True)
                if os.path.islink(p):
                    os.symlink(os.readlink(p), target)
                    symlinks[stage_name(p)] = os.readlink(p)
                elif p.is_dir():
                    target.mkdir(exist_ok=True)
                elif p.is_file():
                    if p.suffix in {".vscdb", ".db", ".sqlite"} and is_sqlite(p):
                        copy_sqlite(p, target)  # consistent copy even mid-write
                    else:
                        shutil.copyfile(p, target)
                    os.chmod(target, st.st_mode & 0o777)
                    files[stage_name(p)] = {"sha256": digest(target), "size": target.stat().st_size,
                                            "mode": st.st_mode & 0o777}
        manifest = dict(report, created=dt.datetime.now(dt.timezone.utc).isoformat(), files=files, symlinks=symlinks)
        (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        shutil.copyfile(__file__, stage / "vscode_migration.py")
        (stage / "RESTORE.md").write_text(RESTORE_GUIDE, encoding="utf-8")
        plain = Path(tmp) / "archive.tar.gz"
        with tarfile.open(plain, "w:gz") as tf:
            for item in sorted(stage.iterdir()):
                tf.add(item, arcname=item.name)
        if args.encrypt:
            subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt",
                            "-in", str(plain), "-out", str(output)] + password_args("VSCODE_MIGRATION_PASSWORD"),
                           check=True)
        else:
            shutil.copyfile(plain, output)
        os.chmod(output, 0o600)
    helper = output.parent / "vscode_migration.py"
    if not helper.exists():
        shutil.copyfile(__file__, helper)
    print(json.dumps({
        "archive": str(output), "bytes": output.stat().st_size, "files": len(files),
        "restore_helper": str(helper),
        "linked_projects_not_copied": [label(x) for x in report["linked"]],
        "copied_because_not_committed_or_pushed": [a["path"] + ": " + "; ".join(a["issues"]) for a in report["attention"]],
        "copied_projects": [r["path"] for r in report["roots"] if r["kind"] == "project"],
        "extensions": {f["name"]: len(f["extensions"]) for f in report["flavors"]},
        "orphaned_project_data": len(report["missing_projects"]),
        "remote_workspaces": report["remote_workspaces"],
    }, indent=2, ensure_ascii=False))


def label(item):
    if item["kind"] == "cloud":
        return item["path"] + "  (" + item["provider"] + ")"
    url = item["remotes"].get("origin") or next(iter(item["remotes"].values()))
    return item["path"] + "  (git: " + url + ", branch " + item["branch"] + ")"


# ---------------------------------------------------------------- restore

def safe_member(name):
    p = PurePosixPath(name)
    return bool(name) and not p.is_absolute() and ".." not in p.parts and "\\" not in name


def open_archive(path, tmp):
    """Decrypt if needed and return the path of a plain .tar.gz."""
    path = absolute(path)
    if path.name.endswith(".enc"):
        plain = Path(tmp) / "archive.tar.gz"
        result = subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
                                 "-in", str(path), "-out", str(plain)] + password_args("VSCODE_MIGRATION_PASSWORD"))
        if result.returncode:
            fail("Wrong password or damaged file.")
        return plain
    return path


def extract(archive, dest):
    """Extract with strict checks and verify every file against the manifest."""
    with tarfile.open(archive, "r:gz") as tf:
        members = tf.getmembers()
        for m in members:
            if not safe_member(m.name) or not (m.isfile() or m.isdir() or m.issym()):
                fail("Unsafe or unsupported archive entry: " + m.name)
            if not (m.name.startswith("files/") or m.name in {"files", "manifest.json", "vscode_migration.py", "RESTORE.md"}):
                fail("Unexpected archive entry: " + m.name)
        manifest = json.load(tf.extractfile("manifest.json"))
        if manifest.get("tool") != TOOL or manifest.get("format") != FORMAT:
            fail("This is not a VS Code migration backup of a supported version.")
        for m in members:
            if m.isfile() and m.name.startswith("files/") and m.name not in manifest["files"]:
                fail("File not listed in the manifest: " + m.name)
        for m in sorted(members, key=lambda x: x.issym()):
            target = dest / m.name
            for parent in target.parents:
                if parent == dest:
                    break
                if parent.is_symlink():
                    fail("Archive writes through a symlink: " + m.name)
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif m.issym():
                if manifest["symlinks"].get(m.name) != m.linkname:
                    fail("Symlink not in manifest: " + m.name)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(m.linkname, target)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with tf.extractfile(m) as source, open(target, "xb") as out:
                    shutil.copyfileobj(source, out, 1 << 20)
    for name, info in manifest["files"].items():
        p = dest / name
        if not p.is_file() or digest(p) != info["sha256"]:
            fail("Checksum mismatch, the backup is damaged: " + name)
        os.chmod(p, info["mode"])
    return manifest


def mappings(values, source_home, home):
    pairs = []
    for value in values:
        old, sep, new = value.partition("=")
        if not sep or not old.startswith("/") or not new.startswith("/"):
            fail("Mappings must be /absolute/old=/absolute/new")
        pairs.append((str(absolute(old)), str(absolute(new))))
    if source_home != str(home) and not any(o == source_home for o, _ in pairs):
        pairs.append((source_home, str(home)))
    return sorted(pairs, key=lambda x: len(x[0]), reverse=True)


def mapped(value, pairs):
    for old, new in pairs:
        if value == old or value.startswith(old + "/"):
            return new + value[len(old):]
    return value


def text_rewriter(pairs):
    """Replace old path prefixes, raw and URI-encoded, only at path boundaries."""
    rules = []
    for old, new in pairs:
        for o, n in {(old, new), (urllib.parse.quote(old), urllib.parse.quote(new))}:
            rules.append((re.compile(re.escape(o) + r"(?=[/\"'\\%?#\s]|$)"), n))
    def rewrite(text):
        for pattern, replacement in rules:
            text = pattern.sub(lambda _m: replacement, text)
        return text
    return rewrite


def remap_staged(stage, manifest, pairs):
    """Rewrite settings/state files that hold project paths. Chat transcripts are left as history."""
    rewrite = text_rewriter(pairs)
    changed = []
    app_roots = [r["path"] for r in manifest["roots"] if r["kind"] == "app"]
    for root in app_roots:
        base = stage / stage_name(root)
        if not base.is_dir():
            continue
        for directory, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in HISTORY_DIRS]
            for name in files:
                p = Path(directory) / name
                if p.is_symlink():
                    continue
                if name.endswith((".json", ".code-workspace")):
                    try:
                        text = p.read_text(encoding="utf-8")
                    except (UnicodeDecodeError, OSError):
                        continue
                    new = rewrite(text)
                    if new != text:
                        p.write_text(new, encoding="utf-8")
                        changed.append(str(p.relative_to(stage)))
                elif is_sqlite(p):
                    con = sqlite3.connect(p)
                    try:
                        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ItemTable'").fetchone():
                            for key, value in con.execute("SELECT key, value FROM ItemTable").fetchall():
                                was_bytes = isinstance(value, bytes)
                                try:
                                    text = value.decode("utf-8") if was_bytes else value
                                except UnicodeDecodeError:
                                    continue
                                if not isinstance(text, str):
                                    continue
                                new = rewrite(text)
                                if new != text:
                                    con.execute("UPDATE ItemTable SET value=? WHERE key=?",
                                                (new.encode("utf-8") if was_bytes else new, key))
                            if con.total_changes:
                                changed.append(str(p.relative_to(stage)))
                        con.commit()
                    finally:
                        con.close()
    return changed


def linked_status(manifest, pairs):
    items = []
    for item in manifest["linked"]:
        dest = absolute(mapped(item["path"], pairs))
        st = dict(item, destination=str(dest))
        if item["kind"] == "cloud":
            st["action"] = "present" if dest.exists() else "waiting for " + item["provider"] + " to sync"
        elif not dest.exists() or (dest.is_dir() and not any(dest.iterdir())):
            st["action"] = "clone"
        else:
            try:
                urls = set(git_out(dest, "remote", "-v").split())
                st["action"] = "present" if urls & set(item["remotes"].values()) else "conflict"
            except (RuntimeError, OSError, subprocess.TimeoutExpired):
                st["action"] = "conflict"
        items.append(st)
    return items


def clone_linked(items, journal, journal_path):
    for st in items:
        if st["kind"] != "git" or st["action"] != "clone":
            continue
        if not shutil.which("git"):
            fail("git is needed to re-create linked projects. Install it with: xcode-select --install")
        dest = Path(st["destination"])
        name = "origin" if "origin" in st["remotes"] else next(iter(st["remotes"]))
        cmd = ["git", "clone", "--origin", name] + (["--branch", st["branch"]] if st["branch"] != "HEAD" else [])
        print("Cloning " + st["remotes"][name] + " -> " + str(dest), flush=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if subprocess.run(cmd + [st["remotes"][name], str(dest)]).returncode:
            fail("Could not clone %s. Check access (for GitHub: gh auth login, or an SSH key), then run again."
                 % st["remotes"][name])
        journal["cloned"].append(str(dest))
        write_json(journal_path, journal)
        for other, url in st["remotes"].items():
            if other != name:
                git_out(dest, "remote", "add", other, url)
        try:
            git_out(dest, "cat-file", "-e", st["head"] + "^{commit}")
            if st["branch"] == "HEAD":
                git_out(dest, "checkout", "--detach", st["head"])
        except RuntimeError:
            journal["warnings"].append("%s: the remote does not have commit %s from the old Mac"
                                       % (dest, st["head"][:12]))
        st["action"] = "cloned"


def rekey(home, journal=None):
    """Rename per-project storage folders to the IDs VS Code computes on this Mac."""
    renamed, conflicts = [], []
    for name, _, _ in FLAVORS:
        support = home / "Library/Application Support" / name
        storage = support / "User/workspaceStorage"
        for d, kind, value in list(workspace_targets(storage)):
            p = uri_to_path(value)
            if p is None or not Path(p).exists():
                continue
            new_id = folder_id(p) if kind == "folder" else workspace_file_id(p)
            if new_id == d.name:
                continue
            target = storage / new_id
            if target.exists():
                conflicts.append("%s: VS Code already created new data for %s; old data kept in %s" % (name, p, d.name))
                continue
            os.rename(d, target)
            backups = support / "Backups"
            if (backups / d.name).is_dir() and not (backups / new_id).exists():
                os.rename(backups / d.name, backups / new_id)
            renamed.append({"project": p, "from": d.name, "to": new_id})
    if journal is not None:
        journal["rekeyed"] = journal.get("rekeyed", []) + renamed
    return renamed, conflicts


def install_extensions(manifest, warnings):
    installed_total = 0
    for flavor in manifest["flavors"]:
        if not flavor["extensions"]:
            continue
        cli = code_cli(flavor["app"])
        if not cli:
            warnings.append("Install %s first, then rerun relink with --apply to add %d extensions"
                            % (flavor["app"], len(flavor["extensions"])))
            continue
        have = set(subprocess.run([cli, "--list-extensions"], capture_output=True, text=True).stdout.lower().split())
        for ext in flavor["extensions"]:
            ident = ext.split("@")[0]
            if ident.lower() in have:
                continue
            print("Installing extension " + ident, flush=True)
            if subprocess.run([cli, "--install-extension", ident], capture_output=True).returncode:
                warnings.append("Could not install extension " + ident)
            else:
                installed_total += 1
    return installed_total


def new_journal(home, kind):
    txid = dt.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    path = home / ".vscode-migration-transactions" / (txid + "-" + kind + ".json")
    journal = {"tool": TOOL, "id": txid, "kind": kind, "status": "started", "operations": [],
               "cloned": [], "warnings": []}
    write_json(path, journal)
    return journal, path


def restore(args):
    home = absolute(args.home)
    with tempfile.TemporaryDirectory(prefix="vscode-restore-", dir=str(home)) as tmp:
        stage = Path(tmp) / "stage"
        stage.mkdir()
        manifest = extract(open_archive(args.archive, tmp), stage)
        pairs = mappings(args.map, manifest["source_home"], home)
        roots = [(r, absolute(mapped(r["path"], pairs))) for r in manifest["roots"]]
        linked = linked_status(manifest, pairs)
        for i, (_, a) in enumerate(roots):
            if a == home or len(a.parts) < 3:
                fail("Unsafe restore destination: " + str(a))
            if any(within(a, b) or within(b, a) for _, b in roots[:i]):
                fail("Restore destinations overlap after mapping: " + str(a))
        preview = {
            "dry_run": not args.apply, "backup_created": manifest["created"],
            "path_mappings": pairs,
            "linked_projects": [{"path": st["destination"], "source": label(st), "action": st["action"]} for st in linked],
            "restore": [{"path": str(d), "kind": r["kind"], "existing_moved_aside": d.exists() or d.is_symlink()}
                        for r, d in roots],
            "extensions_to_install": {f["name"]: len(f["extensions"]) for f in manifest["flavors"]},
            "orphaned_project_data": len(manifest["missing_projects"]),
            "remote_workspaces": manifest["remote_workspaces"],
        }
        print(json.dumps(preview, indent=2, ensure_ascii=False), flush=True)
        if not args.apply:
            return
        if not args.force:
            require_closed()
        conflicts = [st["destination"] for st in linked if st["action"] == "conflict"]
        if conflicts:
            fail("These folders exist but are not clones of the expected repository. Move them aside or use --map: "
                 + ", ".join(conflicts))
        changed = remap_staged(stage, manifest, pairs) if pairs else []
        journal, journal_path = new_journal(home, "restore")
        journal["remapped_files"] = changed
        try:
            clone_linked(linked, journal, journal_path)
            for root, dest in roots:
                src = stage / stage_name(root["path"])
                if not (src.exists() or src.is_symlink()):
                    continue
                op = {"destination": str(dest), "saved": None}
                if dest.exists() or dest.is_symlink():
                    op["saved"] = str(dest.with_name(dest.name + ".pre-migration-" + journal["id"]))
                    os.replace(dest, op["saved"])
                journal["operations"].append(op)
                write_json(journal_path, journal)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dest))
            renamed, rekey_conflicts = rekey(home, journal)
            journal["warnings"] += rekey_conflicts
            if not args.skip_extensions:
                install_extensions(manifest, journal["warnings"])
            journal["status"] = "complete"
        except BaseException:
            journal["status"] = "interrupted"
            write_json(journal_path, journal)
            print("Restore stopped. Undo with: python3 vscode_migration.py rollback " + str(journal_path), file=sys.stderr)
            raise
        write_json(journal_path, journal)
    print("Restored. Journal (for rollback): " + str(journal_path))
    print("Project data re-filed for %d folder(s)." % len(renamed))
    for warning in journal["warnings"]:
        print("WARNING: " + warning)
    for st in linked:
        if st["kind"] == "cloud" and st["action"] != "present":
            print("WAITING: %s is not here yet. Sign in to %s, let it sync, then run relink --apply."
                  % (st["destination"], st["provider"]))
    print("Next: open VS Code and sign in to GitHub/Copilot. Turn on Settings Sync only after this restore.")


def relink(args):
    """Later step: clone repos that were skipped and re-file project data once folders exist."""
    home = absolute(args.home)
    with tempfile.TemporaryDirectory(prefix="vscode-relink-", dir=str(home)) as tmp:
        with tarfile.open(open_archive(args.archive, tmp), "r:gz") as tf:
            manifest = json.load(tf.extractfile("manifest.json"))
    pairs = mappings(args.map, manifest["source_home"], home)
    linked = linked_status(manifest, pairs)
    print(json.dumps({"dry_run": not args.apply,
                      "linked_projects": [{"path": s["destination"], "action": s["action"]} for s in linked]},
                     indent=2, ensure_ascii=False), flush=True)
    if not args.apply:
        return
    if not args.force:
        require_closed()
    journal, journal_path = new_journal(home, "relink")
    clone_linked([s for s in linked if s["action"] != "conflict"], journal, journal_path)
    renamed, conflicts = rekey(home, journal)
    journal["warnings"] += conflicts
    if not args.skip_extensions:
        install_extensions(manifest, journal["warnings"])
    journal["status"] = "complete"
    write_json(journal_path, journal)
    print("Cloned %d project(s); re-filed data for %d folder(s)." % (len(journal["cloned"]), len(renamed)))
    for warning in journal["warnings"]:
        print("WARNING: " + warning)


def rollback(args):
    path = absolute(args.journal)
    journal = json.loads(path.read_text(encoding="utf-8"))
    print(json.dumps({"dry_run": not args.apply, "operations": journal["operations"], "cloned": journal["cloned"]},
                     indent=2, ensure_ascii=False))
    if not args.apply:
        return
    if not args.force:
        require_closed()
    tag = ".after-migration-" + journal["id"]
    for op in reversed(journal["operations"]):
        dest = Path(op["destination"])
        if dest.exists() or dest.is_symlink():
            os.replace(dest, dest.with_name(dest.name + tag))
        if op["saved"] and Path(op["saved"]).exists():
            os.replace(op["saved"], dest)
    for cloned in reversed(journal["cloned"]):
        dest = Path(cloned)
        if dest.exists():
            os.replace(dest, dest.with_name(dest.name + tag))
    journal["status"] = "rolled_back"
    write_json(path, journal)
    print("Rolled back. Restored data was moved to *%s folders, not deleted." % tag)


# ---------------------------------------------------------------- CLI

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    sp = sub.add_parser("inventory", help="Read-only: what would be backed up, linked or flagged")
    sp.add_argument("--deep", action="store_true", help="Also measure size and check for cloud placeholders")
    sp.add_argument("--output", help="Write the full report as JSON")
    sp = sub.add_parser("backup")
    sp.add_argument("--output", required=True)
    sp.add_argument("--encrypt", action="store_true", help="AES-256 with a password (openssl); output must end in .tar.gz.enc")
    for name in ("inventory", "backup"):
        sub.choices[name].add_argument("--include", action="append", default=[], help="Extra folder to copy; repeatable")
    for name in ("restore", "relink"):
        sp = sub.add_parser(name)
        sp.add_argument("archive")
        sp.add_argument("--map", action="append", default=[], help="/old/path=/new/path; repeatable")
        sp.add_argument("--apply", action="store_true", help="Make changes (default is a preview)")
        sp.add_argument("--skip-extensions", action="store_true")
    sp = sub.add_parser("rollback")
    sp.add_argument("journal")
    sp.add_argument("--apply", action="store_true")
    for sp in sub.choices.values():
        sp.add_argument("--home", default=str(Path.home()), help=argparse.SUPPRESS)
        sp.add_argument("--force", action="store_true", help="Skip the VS Code running check (not recommended)")
    args = p.parse_args(argv)
    if args.command == "inventory":
        report = inventory(absolute(args.home), args.include)
        if args.deep:
            report["copy_bytes"] = measure(report)
        if args.output:
            write_json(absolute(args.output), report)
        print(json.dumps({
            "vscode": [f["name"] + " " + str(f["version"]) + ", " + str(len(f["extensions"])) + " extensions" for f in report["flavors"]],
            "linked_projects_not_copied": [label(x) for x in report["linked"]],
            "needs_commit_or_push": [{"path": a["path"], "issues": a["issues"]} for a in report["attention"]],
            "copied_projects": [r["path"] for r in report["roots"] if r["kind"] == "project"],
            "copied_app_data": [r["path"] for r in report["roots"] if r["kind"] == "app"],
            "local_only_files_kept": [r["path"] for r in report["roots"] if r["kind"] == "local-only"],
            "orphaned_project_data": [m["path"] for m in report["missing_projects"]],
            "remote_workspaces": report["remote_workspaces"],
            "warnings": report["warnings"],
            "copy_bytes": report.get("copy_bytes"),
        }, indent=2, ensure_ascii=False))
    elif args.command == "backup":
        backup(args)
    elif args.command == "restore":
        restore(args)
    elif args.command == "relink":
        relink(args)
    else:
        rollback(args)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError, sqlite3.Error, tarfile.TarError,
            subprocess.CalledProcessError) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        sys.exit(1)
