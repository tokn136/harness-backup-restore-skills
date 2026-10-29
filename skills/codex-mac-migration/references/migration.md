# Operating guide

## Commands

Set a task-specific variable to the installed script. For a custom CODEX_HOME,
use its actual skills directory instead. All examples are run in macOS Terminal.

```bash
migration_script="$HOME/.codex/skills/codex-mac-migration/scripts/migration.py"
python3 "$migration_script" inventory --deep --output "$HOME/Documents/codex-inventory.json"
```

Resolve the inventory report before proceeding. Exit code 2 means the deep scan
was incomplete; it retains the exact failure in `deep_scan_error`. Finder may
need to download OneDrive/iCloud files, and Terminal may need access to protected
folders. Do not exclude unreadable projects merely to make a backup succeed.

Quit Codex and the CLI. Stop terminals editing project files; stop a VM if
explicitly including its disk. Prepare enough free space: staging plus compression
and finalization can temporarily require about three times the source file size.
Backups include caches and plugins so large archives are expected.

```bash
python3 "$migration_script" backup \
  --output "$HOME/Documents/Codex Migration Backups/codex-backup.tar.gz"
```

The output folder must be completely separate from all source roots. An existing
archive is never overwritten. Use a new filename for each run. To include an
external dependency, add `--include /absolute/path`; repeat for additional roots.
Inventory can use the same arguments. Broad paths such as the entire home or a
filesystem root are refused. No gitignore rules are applied.

For encryption, install the maintained `age` tool from its official project,
then run interactively:

```bash
python3 "$migration_script" backup --encrypt \
  --output "$HOME/Documents/Codex Migration Backups/codex-backup.tar.gz.age"
```

Store the passphrase separately; the archive cannot recover a lost passphrase.
The tool verifies the plaintext archive before age encrypts it; `.sha256` describes
the final encrypted file. Plaintext staging is private and removed on normal exit;
this does not promise secure erasure on APFS/SSD. Use FileVault for disk protection.

Transfer the archive and its sidecars to the new Mac. The archive itself includes
`migration.py`, `RESTORE.md`, a manifest, and payload files. The skill does not have
to be installed on the destination. Python is required and is not bundled.

```bash
shasum -a 256 /path/to/codex-backup.tar.gz
python3 "$migration_script" verify /path/to/codex-backup.tar.gz
python3 "$migration_script" restore /path/to/codex-backup.tar.gz
python3 "$migration_script" restore /path/to/codex-backup.tar.gz --apply
```

Compare the first command with the original `.sha256`. Checksums detect corruption,
not an attacker replacing both the archive and checksum. Transfer from a trusted
source. Verify/restore accept `.age` and prompt using age in Terminal. To retrieve
the embedded helper from an encrypted archive before the skill is installed,
decrypt with `age -d -o backup.tar.gz backup.tar.gz.age`, then follow RESTORE.md.

## Restoration behavior

Restoration verifies archive entries and rejects traversal, duplicate paths,
hardlink archive records, and writes through symlink entries. It stages payload
on each destination volume, checks extracted file hashes and SQLite integrity,
restores permissions, timestamps and extended attributes, and commits roots via
renames. Parent directories must not be symlinks. Existing destination roots are
moved to sibling `.NAME.codex-rollback-TRANSACTION` paths. New data does not merge
into old databases or project folders.

The durable journal is `~/.codex-migration-transactions/TRANSACTION.json`. Keep it
and rollback folders until the restored app is checked. A failed or interrupted
commit can leave some roots restored; the printed journal supports recovery:

```bash
python3 "$migration_script" rollback /path/to/transaction.json
python3 "$migration_script" rollback /path/to/transaction.json --apply
```

Quit Codex before rollback. The helper restores original roots and moves migrated
contents to `.after-migration-TRANSACTION` siblings, preserving subsequent changes.
It does not delete those copies automatically. A conflict stops recovery rather
than overwriting a previous recovery copy. Restore/rollback import recognized app
preferences with macOS `defaults` to synchronize its preference cache. If that
fails, the journal contains a manual follow-up command.

Use the same installed app version where possible. Version detection uses bundle
identifier `com.openai.codex`, including installations named `ChatGPT.app`. A
known mismatch blocks actual restoration unless explicitly accepted with
`--accept-version-mismatch`. Missing version information is reported as unknown,
not a compatibility guarantee. Same-version destination launch remains necessary.

## Different username or project locations

```bash
python3 "$migration_script" restore /path/to/backup.tar.gz \
  --map /Users/oldname=/Users/newname
# Inspect the preview, then repeat with --apply.
python3 "$migration_script" scan-references --old /Users/oldname \
  "$HOME/.codex" "$HOME/Library/Application Support/Codex"
```

Mappings operate on complete path prefixes, with the longest matching prefix
first. Supply additional explicit mappings for external volumes or moved projects.
The destination home mapping must match the actual current home. Overlapping
mapped restore roots are refused.

Recognized updates: global-state JSON keys/values; SQLite threads.cwd,
threads.rollout_path, threads.sandbox_policy, project_roots.path,
projects.metadata, attachment identities/payloads; leading session_meta.payload.cwd
in active/archived rollouts; automation `cwds` arrays; absolute symlink targets;
Git .git, gitdir, and commondir pointers. SQLite indexes are updated transactionally.
The historical messages themselves are not rewritten. Remapping keeps original
data in the archive and preexisting destination data in rollback copies.

Config TOML, plugin configuration, browser databases, scripts, shell snapshots,
old context messages, and other opaque files may retain old paths. The reference
scan reports filenames only, including legitimate historical occurrences. Inspect
operational files individually. Do not run a global search-and-replace on transcripts
or binary databases. Changed usernames need additional checking; an untouched
filesystem copy alone cannot guarantee portability of all opaque application data.

## Coverage and limits

Discovery reads actual local project registrations and all active/archived thread
working directories in local state DBs, worktree archives, external rollout paths,
projectless outputs, Git worktree common repositories, CODEX_HOME and per-user
app support/preferences. A custom CODEX_HOME can be supplied with `--codex-home`.
The `--home` option changes discovery's home root; it does not impersonate a user
or change restore ownership. Restored files belong to the user running restoration.

Archives retain Git objects/history, uncommitted changes, ignored files, local
transcripts, history databases/WALs, attachments, local memories/goals, preferences,
skills/plugins, browser state and generated artifacts present in included roots.
The manifest lists every entry, SHA-256 for regular files, sizes/modes/timestamps,
extended attributes, symlink targets, SQLite checks/counts, missing paths and omissions.
Git ignored files can include secrets; auth.json and browser state can also be sensitive.

Only identified runtime coordination objects, browser Singleton locks, Unix
sockets and rebuildable SQLite `-shm` indexes are omitted and recorded. The
SQLite main databases and their WALs are retained together. Hardlinked regular
files are stored as separate content copies. File content, permissions, timestamps,
symlinks and ordinary extended attributes are preserved; inode numbers, original
ownership, ACLs, APFS clones/snapshots and filesystem flags are not cloned.

External symlink targets are preserved as links and reported unless included.
Broken historical paths or external dependencies make discovery incomplete and
require resolution or explicit `--allow-incomplete`. Read failures, placeholders,
source changes and corrupt databases always stop backup. Logs/caches are retained;
their presence does not make OS-encrypted credentials portable.

Cloud-only ChatGPT/Codex projects, chats, hosted files and remote machines are not
independently exported. Local caches are not a complete cloud archive. The user
must retain access to the original account/services. Keychain, OAuth grants tied
to the machine, macOS permissions, running processes and external runtimes are
outside a filesystem backup. Review remote-control enrollment and reauthenticate
integrations on the destination. Keep the old app quit while migrating.

Lima VMs (`~/.lima`) are detected as an external dependency if present.
Use its own migration workflow, or explicitly include the stopped VM's files after
checking space. Do not imply plugin skill files contain a whole VM or remote service.

Sources checked during implementation: [OpenAI local state/configuration](https://learn.chatgpt.com/docs/config-file/config-advanced#config-and-state-locations).
The private desktop storage layout is version-dependent; offline copying is a
custom migration tool, not a documented official import API.
