#!/bin/bash
# Link the skills in this repo into the right tool's skills folder:
#   skills with agents/openai.yaml -> ~/.codex/skills, all others -> ~/.claude/skills
# Usage: install.sh [--copy]   (--copy copies instead of symlinking)
set -euo pipefail
REPO="$(cd "$(dirname "$0")" && pwd)"
for skill in "$REPO"/skills/*/; do
  skill="${skill%/}"
  name="$(basename "$skill")"
  if [ -f "$skill/agents/openai.yaml" ]; then base="$HOME/.codex"; else base="$HOME/.claude"; fi
  dest="$base/skills"
  target="$dest/$name"
  mkdir -p "$dest"
  if [ -L "$target" ]; then
    rm "$target"
  elif [ -e "$target" ]; then
    # Keep the old copy outside the skills folder so the tool doesn't load it twice.
    saved="$base/skills-backups/$name-$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$base/skills-backups"
    mv "$target" "$saved"
    echo "Existing $name moved to $saved"
  fi
  if [ "${1:-}" = "--copy" ]; then cp -R "$skill" "$target"; else ln -s "$skill" "$target"; fi
  echo "Installed $name -> $target"
done
