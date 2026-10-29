#!/bin/bash
# Back up local Claude app + Claude Code data into one (optionally encrypted) archive.
# Usage: backup.sh [--out DIR] [--no-encrypt] [--with-ui-state] [--dry-run] [--force]
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "$0")" && pwd)"
MANIFEST="$SKILL_DIR/manifest.txt"
OUT_DIR="$HOME/Desktop"
ENCRYPT=1
UI_STATE=0
DRY_RUN=0
FORCE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT_DIR="$2"; shift 2 ;;
    --no-encrypt) ENCRYPT=0; shift ;;
    --with-ui-state) UI_STATE=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --force) FORCE=1; shift ;;
    -h|--help) sed -n '2,3p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

# 1. The app should be closed so its files are not changing mid-copy.
if [ "$FORCE" = 0 ] && [ "$DRY_RUN" = 0 ] && pgrep -xq "Claude"; then
  echo "The Claude app is still running. Quit it (Cmd+Q) and run this again." >&2
  echo "(Use --force to back up anyway; files in use may be slightly inconsistent.)" >&2
  exit 2
fi

# 2. Read the manifest.
INCLUDES=(); EXCLUDES=()
while IFS= read -r line || [ -n "$line" ]; do
  line="${line%%$'\r'}"
  [[ -z "$line" || "$line" == \#* ]] && continue
  if [[ "$line" == "- "* ]]; then EXCLUDES+=("--exclude=${line#- }")
  else INCLUDES+=("$line"); fi
done < "$MANIFEST"
if [ "$UI_STATE" = 1 ]; then
  INCLUDES+=("Library/Application Support/Claude/IndexedDB"
             "Library/Application Support/Claude/Local Storage")
fi

EXISTING=()
for p in "${INCLUDES[@]}"; do [ -e "$HOME/$p" ] && EXISTING+=("$p"); done

if [ "$DRY_RUN" = 1 ]; then
  echo "Would back up (relative to $HOME):"
  for p in "${EXISTING[@]}"; do printf '  %-75s %s\n' "$p" "$(du -sh "$HOME/$p" 2>/dev/null | cut -f1)"; done
  echo "Excluded patterns: ${EXCLUDES[*]#--exclude=}"
  exit 0
fi

STAMP="$(date +%Y-%m-%d_%H%M)"
NAME="claude-backup-$STAMP"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
STAGE="$WORK/$NAME"
mkdir -p "$STAGE/home" "$STAGE/meta"

# 3. Copy files into the staging folder (paths stay relative to HOME).
echo "Copying files..."
( cd "$HOME" && rsync -a --relative "${EXCLUDES[@]}" "${EXISTING[@]}" "$STAGE/home/" )

# 4. Metadata for restore.
{
  echo "created=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "source_home=$HOME"
  echo "source_user=$(id -un)"
  echo "source_host=$(scutil --get ComputerName 2>/dev/null || hostname)"
  echo "macos=$(sw_vers -productVersion)"
  echo "app_version=$(defaults read /Applications/Claude.app/Contents/Info.plist CFBundleShortVersionString 2>/dev/null || echo unknown)"
  echo "ui_state=$UI_STATE"
} > "$STAGE/meta/info.txt"
( cd "$STAGE/home" && find . -type f -print0 | xargs -0 shasum -a 256 ) > "$STAGE/meta/checksums.sha256"
cp "$SKILL_DIR/restore.sh" "$STAGE/restore.sh"

# 5. Compress (gzip: works on any Mac without extra tools).
mkdir -p "$OUT_DIR"
ARCHIVE="$OUT_DIR/$NAME.tar.gz"
echo "Compressing..."
tar -czf "$ARCHIVE" -C "$WORK" "$NAME"; chmod 600 "$ARCHIVE"

# 6. Encrypt (AES-256). Password from $CLAUDE_BACKUP_PASSWORD or typed at the prompt.
if [ "$ENCRYPT" = 1 ]; then
  if [ -n "${CLAUDE_BACKUP_PASSWORD:-}" ]; then PASS=(-pass env:CLAUDE_BACKUP_PASSWORD)
  else echo "Choose a password for the backup (you'll need it on the new Mac):"; PASS=(); fi
  openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -in "$ARCHIVE" -out "$ARCHIVE.enc" "${PASS[@]+"${PASS[@]}"}"; chmod 600 "$ARCHIVE.enc"
  rm -f "$ARCHIVE"
  ARCHIVE="$ARCHIVE.enc"
fi

# Put a copy of the restore script next to the archive so the new Mac needs nothing else.
cp "$SKILL_DIR/restore.sh" "$OUT_DIR/restore-claude-backup.sh"
chmod +x "$OUT_DIR/restore-claude-backup.sh"

FILES=$(wc -l < "$STAGE/meta/checksums.sha256" | tr -d ' ')
echo
echo "Done."
echo "  Archive: $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1), $FILES files)"
echo "  Restore script: $OUT_DIR/restore-claude-backup.sh"
echo
echo "Copy BOTH files to the new Mac, then run:"
echo "  bash restore-claude-backup.sh \"$(basename "$ARCHIVE")\""
