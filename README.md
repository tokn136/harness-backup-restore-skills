# Harness backup & restore skills

Claude skills that back up the local data of AI coding tools into one encrypted archive and restore it on a new Mac, fixing file paths when your username or project folders change.

| Skill | Tool | Status |
|---|---|---|
| [`claude-migrate`](skills/claude-migrate/SKILL.md) | Claude desktop app + Claude Code | ✅ Ready |
| [`codex-mac-migration`](skills/codex-mac-migration/SKILL.md) | Codex desktop app + CLI | ✅ Ready |

## Projects in git or the cloud

Projects that already live somewhere else are linked, not copied:

- **Cloud folders** (OneDrive, Dropbox, Google Drive, iCloud Drive) arrive on the new Mac when you sign in to that service.
- **Git repositories** with everything committed and pushed are cloned back to the same path and branch. Local-only ignored files like `.env` are kept. Rebuildable folders like `node_modules` are skipped.
- **Repositories with uncommitted, unpushed or stashed work** are flagged before the backup so you can commit and push first. If you don't, they're copied as files.

The Codex skill does this today. The Claude skill doesn't back up project folders yet.

## Install

```bash
git clone https://github.com/tokn136/harness-backup-restore-skills.git
cd harness-backup-restore-skills
bash install.sh
```

This links each skill into `~/.claude/skills/`. Then ask Claude to "back up Claude for a new Mac".

## Use without Claude

The scripts also run on their own:

```bash
# Old Mac (quit the app first)
bash skills/claude-migrate/backup.sh            # --dry-run to preview

# New Mac (copy both files from the Desktop over)
bash restore-claude-backup.sh claude-backup-<date>.tar.gz.enc
```

## What's not included

- Cloud data such as claude.ai chats, Projects and memory. It comes back when you sign in.
- Login tokens and cookies. Sign in again on the new Mac.
- Caches, VM images and app binaries. They're downloaded again.
- Your project folders. Copy them or clone them from git.
