---
name: claude-mac-migration
description: Back up all local Claude app and Claude Code data (session history, Code-tab sessions, settings, MCP servers, plugins, skills, plans) into one encrypted archive, and restore it on a new Mac with automatic path fixing. Use for "backup Claude", "move Claude to a new Mac", "migrate Claude", "restore Claude backup", "export Claude sessions".
---

# Claude Mac migration (backup and restore)

Scripts live next to this file: `backup.sh`, `restore.sh`, `manifest.txt`.

## What is and isn't covered

- claude.ai chats, Projects, memory and account skills are stored in the Anthropic account.
  They come back when the user signs in on the new Mac. They are NOT in the archive.
- The archive holds local data: `~/.claude` (Claude Code history, plugins, agents, skills, plans),
  `~/.claude.json`, the desktop app's config and MCP files, Code-tab / Cowork sessions and
  "No folder" workspaces. The paths are listed in `manifest.txt`.
- Skipped: the 11+ GB VM image, app binaries, caches, logs and login cookies (re-created or re-downloaded).
- Project folders themselves (e.g. `~/Developer/...`) are not included. Tell the user to copy them or clone them from git.

## Backup (old Mac)

1. Run `bash ~/.claude/skills/claude-mac-migration/backup.sh --dry-run` and show the user what will be included.
2. The app must be closed during the real backup, and the password prompt needs a real terminal.
   If Claude itself is running inside the app, do NOT run the real backup from the Bash tool.
   Give the user these steps instead:
   - Quit Claude (Cmd+Q)
   - In Terminal.app run: `bash ~/.claude/skills/claude-mac-migration/backup.sh`
     (options: `--out DIR` (default Desktop), `--no-encrypt`, `--with-ui-state`)
3. The result is two files: `claude-backup-<date>.tar.gz.enc` and `restore-claude-backup.sh`.
   Both go to the new Mac (AirDrop, USB stick, cloud drive).

## Restore (new Mac)

1. Install Claude, open it, sign in once, then quit it (Cmd+Q).
2. In Terminal: `bash restore-claude-backup.sh claude-backup-<date>.tar.gz.enc`
   - The old home path is replaced with the new one automatically (for a different username).
   - If project folders moved: add `--map /Users/old/Developer=/Users/new/Code` (repeatable).
   - `--dry-run` previews; `--home DIR` restores into a test folder.
   - Files it overwrites are saved to `~/claude-pre-restore-<timestamp>/`.
3. Open Claude, sign in, reconnect connectors, and copy the project folders to the same paths.
