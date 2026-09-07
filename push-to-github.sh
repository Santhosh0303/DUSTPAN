#!/usr/bin/env bash
# Creates a PRIVATE GitHub repo named "dustpan" and pushes this commit to it.
# Requires the GitHub CLI, authenticated:  gh auth login
set -euo pipefail
gh repo create dustpan --private --source . --remote origin --push
echo "done: $(gh repo view --json url --jq .url 2>/dev/null || echo 'https://github.com/Santhosh0303/dustpan')"
