#!/usr/bin/env bash
# Publish skills/regress-runbook to the public skill repo TrueForge loads it from; prints the commit SHA.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="${SKILL_REPO:-PradeepKundekar0101/regress-runbook}"
WORK=.regress/skill-repo
if [ ! -d "$WORK/.git" ]; then
  rm -rf "$WORK"
  gh repo clone "$REPO" "$WORK" -- --quiet
fi
git -C "$WORK" pull --quiet --ff-only || true
mkdir -p "$WORK/skills"
rm -rf "$WORK/skills/regress-runbook"
cp -R skills/regress-runbook "$WORK/skills/regress-runbook"
find "$WORK/skills" -name "__pycache__" -prune -exec rm -rf {} +
git -C "$WORK" add -A
if ! git -C "$WORK" diff --cached --quiet; then
  git -C "$WORK" commit --quiet -m "Update regress-runbook skill"
  git -C "$WORK" push --quiet origin HEAD
fi
git -C "$WORK" rev-parse HEAD
