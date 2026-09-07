#!/usr/bin/env bash
# AUD-V1-014: the previous release ZIP was the working directory, so it
# shipped 151 loose Git objects, the index, reflogs, config (with a personal
# email address) and COMMIT_EDITMSG (with a session URL) -- 174 of its 229
# files were repository internals. A release archive is an export of tracked
# content at a commit, never a copy of the working tree.
set -euo pipefail
ref="${1:-HEAD}"
out="${2:-dustpan-release.zip}"
git archive --format=zip --prefix=dustpan/ -o "$out" "$ref"
echo "wrote $out from $(git rev-parse --short "$ref")"
python3 - "$out" <<'PY'
import sys, zipfile
names = zipfile.ZipFile(sys.argv[1]).namelist()
leaks = [
    n
    for n in names
    if "/.git/" in n
    or "egg-info" in n
    or "__pycache__" in n
    or n.endswith((".pyc", ".env"))
]
print(f"  {len(names)} entries; repository/generated internals: {len(leaks)}")
sys.exit(1 if leaks else 0)
PY
