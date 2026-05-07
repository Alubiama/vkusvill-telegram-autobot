#!/bin/bash
# Publish webapp/ to Alubiama/vkusvill-telegram-autobot (GitHub Pages via workflow)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${ROOT}/.pages-checkout"
REMOTE="git@github.com:Alubiama/vkusvill-telegram-autobot.git"

export HOME="${HOME:-/root}"
export GIT_SSH_COMMAND="ssh -i ${HOME}/.ssh/github_vps -o StrictHostKeyChecking=accept-new"

if [ ! -d "${WORK}/.git" ]; then
    rm -rf "${WORK}"
    git clone --depth 1 --branch main --no-checkout "${REMOTE}" "${WORK}"
    cd "${WORK}"
    git sparse-checkout init --cone
    git sparse-checkout set webapp
    git checkout main
fi

cd "${WORK}"
git fetch origin main
git reset --hard origin/main

rsync -a --delete "${ROOT}/webapp/" "${WORK}/webapp/"

if git diff --quiet && git diff --cached --quiet; then
    echo "no changes"
    exit 0
fi

git add -A webapp/
git -c user.name="vkusvill-bot" -c user.email="bot@alubiama.local" \
    commit -m "publish mini-app $(date -u +%Y-%m-%dT%H:%M:%SZ)"
git push origin main
echo "published"
