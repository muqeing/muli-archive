#!/usr/bin/env bash
# Publish the archive console / classifier source to GitHub.
# The workspace directory itself is the git working copy.
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"
python3 - <<'PY'
import ast, pathlib, sys
bad = []
for path in sorted(pathlib.Path('src').rglob('*.py')):
    try:
        ast.parse(path.read_text())
    except SyntaxError as exc:
        bad.append('%s: %s' % (path, exc))
if bad:
    sys.exit('syntax gate failed:\n' + '\n'.join(bad))
print('syntax gate ok')
PY
MSG="$1"
if [ -z "$MSG" ]; then MSG="Update archive console and classifier"; fi
git add -A
if git diff --cached --quiet; then echo 'nothing to publish'; exit 0; fi
git commit -m "$MSG"
git push origin main
