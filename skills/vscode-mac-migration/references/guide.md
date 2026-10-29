# VS Code migration guide

## What is backed up

| Data | Location | How |
|---|---|---|
| Settings, keybindings, snippets, tasks, MCP config, profiles | `~/Library/Application Support/Code/User` | Copied |
| Copilot chat history and per-project state | `User/workspaceStorage/<id>`, `User/globalStorage` | Copied; re-filed on restore |
| Local file history, unsaved editors, untitled workspaces | `User/History`, `Backups`, `Workspaces` | Copied |
| Agent sessions | `agentSessionData` | Copied |
| Extensions | `~/.vscode/extensions` | Only the list; reinstalled from the Marketplace |
| `argv.json`, agent plugins | `~/.vscode` | Copied |
| GitHub Copilot CLI | `~/.copilot` (without `logs`) | Copied |
| Projects | Folders VS Code has opened | Linked or copied (below) |

VS Code Insiders (`Code - Insiders`, `~/.vscode-insiders`) is handled the same way if present.

Not included: caches, logs, downloaded extension files, sign-ins (stored in the macOS Keychain),
and any `User.backup-*` or similar folders next to `User`.

## Projects

| Group | Rule | Archive | Restore |
|---|---|---|---|
| Cloud | In `~/Library/CloudStorage/*` (OneDrive, Dropbox, Google Drive, Box) or iCloud Drive | Not copied | Reported as waiting until synced |
| Git, linked | Remote set, clean, all branches and HEAD pushed (per last fetch), no stashes, not a worktree | Only git-ignored local files, minus rebuildable folders | Cloned to the same (mapped) path and branch; extra remotes re-added; local files put back |
| Copied | Everything else | Full copy including `.git`, minus `node_modules`, `.venv`, caches | Restored as files |

Projects come from `workspace.json` in each `workspaceStorage` folder, plus folders listed in
`.code-workspace` files. Nested projects are covered by their parent. `--include /path` adds a
folder and always copies it.

## Why chat history needs re-filing

VS Code stores each single-folder workspace's data under
`md5(folder path + folder creation time in ms)`, and each `.code-workspace` under
`md5(lowercased file path)`. A fresh clone or copy on the new Mac has a new creation time, so
VS Code would look in a new, empty folder. After putting files in place, restore computes the
new ID for every folder that exists and renames the storage folder (and its hot-exit backup).
If VS Code already made data for that folder, the old data is kept under its old name and a
warning is printed. `relink --apply` does the same later for folders that arrive afterwards.

## Paths

When the home folder or a project path changes, these are rewritten (raw and `file://` forms,
only at path boundaries): JSON files in the app data (`workspace.json`, `storage.json`,
`settings.json`, local history `entries.json`, ...) and values in `state.vscdb` key-value
tables. Chat transcripts (`chatSessions`, `chatEditingSessions`, `emptyWindowChatSessions`) are
left exactly as they were.

## Safety

- Backup refuses to run while VS Code is open and copies SQLite databases with SQLite's backup
  API. Files that a cloud service has not downloaded stop the backup with a clear message.
- The archive includes a manifest with a SHA-256 for every file. Restore rejects absolute paths,
  `..`, unknown entries, files not in the manifest, and writes through symlinks, and checks all
  checksums before changing anything.
- Encryption: `openssl enc -aes-256-cbc -pbkdf2 -iter 200000`. The password is typed at the
  prompt (or read from `VSCODE_MIGRATION_PASSWORD` for automation); it is never stored.
- Restore moves anything it replaces to `<name>.pre-migration-<id>`. The journal in
  `~/.vscode-migration-transactions/` drives `rollback`, which moves restored data and fresh
  clones to `<name>.after-migration-<id>` and puts the originals back. Nothing is deleted.

## Settings Sync

Settings Sync covers settings, keybindings, snippets, extensions and profiles, but not chat
history, workspace state or projects. Turn it on only after restoring, so it doesn't overwrite
the restored files with older cloud data.
