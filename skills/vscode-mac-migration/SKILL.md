---
name: vscode-mac-migration
description: Back up VS Code (settings, keybindings, snippets, extension list, Copilot chat history, per-project state, Copilot CLI data) and the projects it knows about, and restore everything on a new Mac. Projects in git or OneDrive/iCloud/Dropbox are linked and re-cloned instead of copied; repos with uncommitted or unpushed work are flagged so they can be committed first. Use for "back up VS Code", "move VS Code to a new Mac", "migrate VS Code", "restore VS Code backup", "keep my Copilot chats".
---

# VS Code Mac migration

Script: `scripts/vscode_migration.py` (Python 3.9+, standard library; encryption uses the macOS
`openssl` command). Details: [the guide](references/guide.md). Every command except `inventory`
previews by default and changes nothing until `--apply` is added.

## Back up (old Mac)

1. Run `python3 scripts/vscode_migration.py inventory --deep` (read-only; VS Code may stay open).
   Show the user, in plain words:
   - `linked_projects_not_copied`: git repos (clean, pushed) are cloned back on the new Mac;
     cloud folders arrive through OneDrive/iCloud/Dropbox. Git-ignored local files such as
     `.env` are kept in the archive (`local_only_files_kept`).
   - `needs_commit_or_push`: repositories with uncommitted, unpushed or stashed work.
   - `copied_projects`: folders copied as files (no git, or not committed/pushed).
     Rebuildable folders such as `node_modules` and `.venv` are skipped.
   - `orphaned_project_data`: VS Code data for folders that no longer exist. Kept, but VS Code
     can only show it again if a folder at that path comes back.
   - `remote_workspaces`: SSH/WSL/container workspaces; their files stay on the remote machine.
2. Go through `needs_commit_or_push` one repository at a time. Show `git status` and the
   unpushed branches and offer to commit and push, so the project is linked instead of copied.
   Commit, push or create a remote only after the user agrees for that repository, with a
   commit message they accept. If they decline, it is copied as files and nothing is lost.
   Run inventory again afterwards. "Pushed" uses the last fetch; run `git fetch` first if the
   remote may have changed.
3. The backup needs VS Code fully quit and a password prompt, so give the user these steps
   to run in Terminal.app rather than running them from inside an editor:
   - Quit VS Code (Cmd+Q).
   - `python3 <skill>/scripts/vscode_migration.py backup --encrypt --output ~/Desktop/vscode-backup.tar.gz.enc`
   Never ask for the password in chat or put it in a command. Without `--encrypt` the archive
   (`.tar.gz`) holds `.env` files and chat history in plain form; say so if the user skips it.
4. The output folder gets the archive plus `vscode_migration.py` (the restore helper). Both go to
   the new Mac.

## Restore (new Mac)

1. Install VS Code, open it once, quit it. Install git (`xcode-select --install`) and sign in to
   GitHub (`gh auth login` or an SSH key) so linked repos can be cloned.
2. In Terminal: `python3 vscode_migration.py restore vscode-backup.tar.gz.enc` to preview, then
   the same with `--apply`. A different home folder (username) is mapped automatically; use
   `--map /old/path=/new/path` only for projects that moved elsewhere.
3. Restore clones linked repos, puts local-only files back into them, copies everything else,
   moves anything it replaces to `*.pre-migration-*`, re-files per-project data (Copilot chats)
   under the IDs VS Code uses for the folders on this Mac, and reinstalls extensions.
4. Cloud folders that have not synced yet are reported as waiting. Once they are there, run
   `relink <archive> --apply`; it also clones repos that were skipped and re-files their data.
   Do not open those folders in VS Code before relink, or VS Code starts them with empty data.
5. Open VS Code, sign in to GitHub/Copilot. Turn on Settings Sync only after restoring.
   `rollback <journal> --apply` undoes a restore; the journal path is printed at the end.
