#!/bin/bash
# Restore a Claude backup made by backup.sh onto this Mac.
# Usage: restore.sh ARCHIVE [--map OLD_PATH=NEW_PATH]... [--home DIR] [--dry-run] [--force]
#   --map   extra path rewrites, e.g. --map /Users/old/Developer=/Users/new/Code
#   --home  restore into another folder instead of $HOME (for testing)
set -euo pipefail

ARCHIVE=""; TARGET_HOME="$HOME"; DRY_RUN=0; FORCE=0; MAPS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --map) MAPS+=("$2"); shift 2 ;;
    --home) TARGET_HOME="${2%/}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --force) FORCE=1; shift ;;
    -h|--help) sed -n '2,5p' "$0"; exit 0 ;;
    *) ARCHIVE="$1"; shift ;;
  esac
done
[ -f "$ARCHIVE" ] || { echo "Archive not found: $ARCHIVE" >&2; exit 1; }

if [ "$FORCE" = 0 ] && [ "$DRY_RUN" = 0 ] && [ "$TARGET_HOME" = "$HOME" ] && pgrep -xq "Claude"; then
  echo "Quit the Claude app (Cmd+Q) first, then run this again." >&2; exit 2
fi

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

# 1. Decrypt if needed, then unpack.
TARBALL="$ARCHIVE"
if [[ "$ARCHIVE" == *.enc ]]; then
  if [ -n "${CLAUDE_BACKUP_PASSWORD:-}" ]; then PASS=(-pass env:CLAUDE_BACKUP_PASSWORD)
  else echo "Enter the backup password:"; PASS=(); fi
  TARBALL="$WORK/backup.tar.gz"
  openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in "$ARCHIVE" -out "$TARBALL" "${PASS[@]+"${PASS[@]}"}" \
    || { echo "Wrong password or damaged file." >&2; exit 3; }
fi
tar -xzf "$TARBALL" -C "$WORK"
STAGE="$(find "$WORK" -maxdepth 1 -type d -name 'claude-backup-*' | head -1)"
[ -d "$STAGE/home" ] || { echo "This does not look like a Claude backup." >&2; exit 3; }

# 2. Verify checksums.
echo "Checking files..."
( cd "$STAGE/home" && shasum -a 256 -c --quiet "$STAGE/meta/checksums.sha256" ) \
  || { echo "Checksum mismatch: the backup is damaged." >&2; exit 3; }

OLD_HOME="$(grep '^source_home=' "$STAGE/meta/info.txt" | cut -d= -f2-)"
echo "Backup from: $(grep -E '^(source_host|created)=' "$STAGE/meta/info.txt" | cut -d= -f2- | paste -sd' ' -)"

# 3. Path rewrites: old home -> new home, plus any --map pairs (more specific first).
PAIRS=()
for m in "${MAPS[@]+"${MAPS[@]}"}"; do PAIRS+=("$m"); done
[ "$OLD_HOME" != "$TARGET_HOME" ] && PAIRS+=("$OLD_HOME=$TARGET_HOME")

# Claude Code names history folders after the project path, with / . and spaces turned into -
encode() { printf '%s' "$1" | sed 's/[^A-Za-z0-9-]/-/g'; }

if [ ${#PAIRS[@]} -gt 0 ]; then
  echo "Rewriting paths:"
  for pair in "${PAIRS[@]}"; do
    OLD="${pair%%=*}"; NEW="${pair#*=}"; echo "  $OLD -> $NEW"
    EOLD="$(encode "$OLD")"; ENEW="$(encode "$NEW")"
    # 3a. Text inside files: plain paths and their encoded folder-name form
    for p in "$OLD=$NEW" "$EOLD=$ENEW"; do
      O="${p%%=*}"; N="${p#*=}"
      grep -rlIF -- "$O" "$STAGE/home" 2>/dev/null | while IFS= read -r f; do
        O="$O" N="$N" perl -pi -e 's/\Q$ENV{O}\E/$ENV{N}/g' "$f"
      done
    done
    # 3b. Encoded folder names in ~/.claude/projects (and similar)
    find "$STAGE/home" -depth -name "${EOLD}*" | while IFS= read -r d; do
      base="$(basename "$d")"; newbase="${ENEW}${base#"$EOLD"}"
      if [ "$base" != "$newbase" ]; then
        mv "$d" "$(dirname "$d")/$newbase" 2>/dev/null \
          || echo "  warning: could not rename $base (name too long?) - kept as is"
      fi
    done
  done
fi

if [ "$DRY_RUN" = 1 ]; then
  echo; echo "Dry run. Would restore into $TARGET_HOME:"
  ( cd "$STAGE/home" && find . -mindepth 1 -maxdepth 4 -not -path './.claude/*/*' | sed 's|^\./|  |' | head -60 )
  exit 0
fi

# 4. Keep a copy of anything that would be overwritten.
SAFE="$TARGET_HOME/claude-pre-restore-$(date +%Y%m%d-%H%M%S)"
( cd "$STAGE/home" && find . -mindepth 1 \( -type f -o -type l \) ) | while IFS= read -r rel; do
  rel="${rel#./}"
  if [ -e "$TARGET_HOME/$rel" ]; then
    mkdir -p "$SAFE/$(dirname "$rel")"; cp -p "$TARGET_HOME/$rel" "$SAFE/$rel"
  fi
done

# 5. Copy into place, fix permissions, clear the quarantine flag.
mkdir -p "$TARGET_HOME"
rsync -a "$STAGE/home/" "$TARGET_HOME/"
chmod 700 "$TARGET_HOME/.claude" 2>/dev/null || true
chmod 600 "$TARGET_HOME/.claude.json" "$TARGET_HOME/Library/Application Support/Claude/"*.json 2>/dev/null || true
xattr -dr com.apple.quarantine "$TARGET_HOME/.claude" 2>/dev/null || true
[ "$TARGET_HOME" = "$HOME" ] && killall cfprefsd 2>/dev/null || true   # reload app preferences

echo
echo "Restore complete into $TARGET_HOME"
[ -d "$SAFE" ] && echo "Files that were replaced are saved in: $SAFE"
cat <<'EOF'

Still to do by hand:
  1. Open Claude and sign in (chats, Projects and memory come from your account).
  2. Reconnect connectors / OAuth (Outlook, Chrome extension, 1Password, etc.).
  3. Copy or git-clone your project folders to the same paths as before
     (or re-run restore with --map OLD=NEW if they live somewhere else).
  4. Install the tools your MCP servers need (node, python, uv, ...)
     - see "Library/Application Support/Claude/claude_desktop_config.json".
EOF
