---
name: codex-mac-migration
description: Back up, verify, or restore Codex desktop data and local projects when moving between Macs. Creates a compressed archive with chat history, app state, project files, skills, and a standalone restore helper. Does not export cloud-only conversations or clone macOS Keychain.
---

# Codex Mac Migration

Use `scripts/migration.py` with Python 3.9+ for deterministic discovery, backup,
verification, restoration, and rollback. It uses the standard library; optional
passphrase encryption requires `age`. Read [the operating guide](references/migration.md)
for command examples, dependencies, path changes, and recovery.

## Prepare and back up

1. Run `inventory --deep --output <report.json>` while Codex is open. Inspect the
   report's roots, missing paths, `deep_scan_complete`, external symlinks, app
   version, and dependency notes. Reports contain paths and schema/count metadata,
   not chat messages or credential values. Resolve errors before claiming full
   coverage. An incomplete deep scan exits with code 2 and retains its report.
2. Show the user how each project will move. Projects that already live
   somewhere else are linked, not copied (`linked_projects_not_copied`):
   - Cloud folders (OneDrive, Dropbox, Google Drive under `~/Library/CloudStorage`,
     iCloud Drive): the sync service brings them to the new Mac.
   - Git repositories with a remote where everything is committed and pushed and
     nothing is stashed: restore clones them back to the same path and branch.
     Git-ignored files that exist only locally (e.g. `.env`) stay in the archive.
     Rebuildable folders such as `node_modules` and `.venv` are skipped.
   Everything else is copied as files, including Codex home, app data and
   projectless outputs. The app is detected by bundle identifier, including
   installations named ChatGPT.app.
3. Go through `needs_commit_or_push` with the user, one repository at a time.
   Show `git status` / unpushed branches and offer to commit and push so the
   project can be linked instead of copied. Commit, push or create a remote only
   after the user agrees for that repository, with a commit message they accept.
   Stashes need to be applied and committed, or kept by copying. If the user
   declines, the repository is copied as files, so nothing is lost. Run
   inventory again after changes. Linking uses the last fetch: run `git fetch`
   first if the remote may have changed.
4. For external symlink targets, add explicit `--include` arguments if the user
   wants the dependencies migrated. Treat remote hosts, Lima VMs, cloud-hosted
   projects, and Keychain as separately reported dependencies. Download cloud
   placeholders before backup. Permission/read failures stop the backup.
5. Choose an archive folder completely outside the discovered source roots,
   normally `~/Documents/Codex Migration Backups`. Provide a reviewable Terminal
   command or launcher. Codex must be completely quit before the helper runs;
   include Codex CLI/server/helper processes and pause other source-file writers.
   Do not quit the active app before handing off the command and instructions.
   Process checks can be blocked by the app sandbox; run in macOS Terminal rather
   than weakening the check. Do not claim a live in-app copy is a consistent backup.
6. Run `backup --output <archive.tar.gz>`, or add `--encrypt` with an `.age` output
   suffix. Encryption prompts in Terminal; never request a passphrase in chat or
   put it in arguments, environment variables, scripts, or logs. Unencrypted
   archives include file-based credentials and project secrets; explain that
   directly when offering the choice.
7. `--allow-incomplete` requires the user's acceptance of the concrete missing
   paths/dependencies. It does not bypass permission errors, corrupt databases,
   cloud placeholders, source changes, or the stopped-app check. Never add it merely
   to obtain a successful exit status.
8. The helper stages files, checks SQLite integrity and source stability, writes a
   compressed archive with an embedded manifest/helper/restore instructions, and
   verifies file checksums. Report archive path, size, checksum, and completeness.
   Keep the archive's `.sha256` and `.report.json` sidecars when transferring it.

## Restore and verify

1. Verify the transferred archive with `verify <archive>`. Inspect its report and
   any exclusions; a successful checksum is not proof of cloud export or a working
   new installation. Compare the external SHA-256 when available.
2. Install a compatible app and Python on the destination Mac. Keep Codex quit.
   Run `restore <archive>` to preview destinations, existing data replacements,
   source/destination app versions, and path mappings. Restoration does not merge
   destination databases; it moves existing roots aside and retains rollback data.
   The preview's `linked_projects` shows each linked project: `clone` (git),
   `present`, `waiting for <provider> to sync` (cloud), or `conflict` (a
   different folder is in the way, which blocks restore until it is moved or mapped).
   Cloning needs access to the remote, e.g. `gh auth login` or an SSH key.
3. When the user has requested actual restoration and the preview matches that
   scope, use `restore <archive> --apply` from Terminal. It clones linked git
   projects first, then puts local-only files (like `.env`) back into them.
   If a cloud folder has not synced yet, run `relink <archive>` later to check,
   and `relink <archive> --apply` to clone repositories that were skipped. A request to inspect or
   preview alone does not authorize replacement. Do not apply a version mismatch
   override without checking compatibility and explaining the concrete mismatch.
4. Prefer the same macOS short username. For a different home/path, use explicit
   `--map /old=/new`. Only known operational fields are remapped; read the guide,
   run `scan-references`, and inspect settings/plugin files before launching.
   Preserve the original transcript text. Unknown SQLite remapping schemas stop.
5. Review restored automations before launching. Keep Codex quit on the old Mac
   to avoid running schedules twice. Restore prints a durable rollback journal;
   retain it and the sibling rollback folders until migration checks pass.
6. Check active/archived chats, sidebar organization, project paths, attachments,
   generated artifacts, skills/plugins, and `git status` in linked worktrees.
   Reauthenticate the same account, reconnect integrations, install runtimes, and
   grant macOS permissions as needed. Report what was verified on this Mac versus
   what still requires the destination Mac. Do not describe migration as complete
   before the destination checks pass.

The reusable helper tests are `scripts/test_migration.py`. They use synthetic
temporary homes and never restore the user's actual state. Run them after code
changes and retain meaningful coverage for WAL recovery, Unicode/permissions,
path mapping, worktrees, corruption, unsafe archives, and rollback.
