#!/usr/bin/env bash
#
# Push this repo to a Hugging Face Docker Space.
#
# Everything the deploy needs is already in the repo - this only wires up the
# remote, swaps in the Space's README (HF requires YAML frontmatter naming the
# SDK and port), and pushes. Secrets are NOT set here: they go in the Space UI
# so they never touch git.
#
# Usage:
#   HF_TOKEN=hf_xxx ./deploy/push-space.sh <hf-username> <space-name>
#
set -euo pipefail

USER="${1:?usage: push-space.sh <hf-username> <space-name>}"
SPACE="${2:?usage: push-space.sh <hf-username> <space-name>}"
: "${HF_TOKEN:?set HF_TOKEN (Settings -> Access Tokens, needs *write* scope)}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ -n "$(git status --porcelain)" ]; then
  echo "working tree is dirty - commit first, the Space builds what you push" >&2
  exit 1
fi

echo "==> building the frontend is not needed; the image does it in stage 1"

# HF reads the Space config from README.md frontmatter. Ours is the project
# README, so the Space version is committed on a throwaway branch that is never
# merged back to main.
BRANCH="space-deploy-$(date +%s)"
git checkout -q -b "$BRANCH"
cp deploy/SPACE_README.md README.md
git add README.md
git -c user.name="Latency Zero" -c user.email="noreply@users.noreply.github.com" \
    commit -q -m "chore(deploy): space manifest"

git remote remove space 2>/dev/null || true
git remote add space "https://${USER}:${HF_TOKEN}@huggingface.co/spaces/${USER}/${SPACE}"

echo "==> pushing to https://huggingface.co/spaces/${USER}/${SPACE}"
git push -q --force space "${BRANCH}:main"

# Leave no credential behind in .git/config.
git remote remove space
git checkout -q main
git branch -q -D "$BRANCH"

cat <<EOF

==> pushed. Now, in the Space UI:

  Settings -> Variables and secrets, add as SECRETS (not variables):
    SUPABASE_URL
    SUPABASE_SERVICE_KEY
    SECRET_KEY
    GROQ_API_KEY          <- required; Ollama cannot run on a Space

  Then watch the build. When it is green:
    curl https://${USER}-${SPACE}.hf.space/health

Rotate SUPABASE_SERVICE_KEY and SECRET_KEY before making the Space public.
EOF
