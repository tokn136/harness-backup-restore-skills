#!/bin/bash
# Link the skills in this repo into ~/.claude/skills so Claude can use them.
# Usage: install.sh [--copy]   (--copy copies instead of symlinking)
set -euo pipefail
REPO="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.claude/skills"
mkdir -p "$DEST"
for skill in "$REPO"/skills/*/; do
  name="$(basename "$skill")"
  target="$DEST/$name"
  if [ -e "$target" ] && [ ! -L "$target" ]; then
    mv "$target" "$target.bak-$(date +%Y%m%d-%H%M%S)"
    echo "Existing $name moved aside."
  fi
  rm -f "$target"
  if [ "${1:-}" = "--copy" ]; then cp -R "$skill" "$target"; else ln -s "${skill%/}" "$target"; fi
  echo "Installed $name -> $target"
done
