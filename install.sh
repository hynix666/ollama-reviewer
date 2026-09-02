#!/usr/bin/env bash
# Link this repo's commands/ into ~/.claude/commands/ollama.
#
# A symlink points at a path, so it survives git checkout and pull - unlike a
# hardlink, which git silently breaks by replacing the file.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target="$repo/commands"
link="$HOME/.claude/commands/ollama"

if [ ! -d "$target" ]; then
    echo "commands/ not found in $repo - is this the right directory?" >&2
    exit 1
fi

mkdir -p "$(dirname "$link")"

if [ -e "$link" ] || [ -L "$link" ]; then
    if [ -L "$link" ]; then
        echo "Replacing existing symlink at $link"
        rm "$link"
    else
        echo "$link already exists and is not a symlink. Move it aside first." >&2
        exit 1
    fi
fi

ln -s "$target" "$link"

count=$(find "$link" -maxdepth 1 -name '*.md' | wc -l | tr -d ' ')
echo "Linked $link -> $target ($count commands)"
echo "Available as: /ollama:review  /ollama:review-file  /ollama:adversarial  /ollama:status"

echo
echo "Running selftest..."
python3 "$repo/scripts/selftest.py"
