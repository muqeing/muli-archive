#!/usr/bin/env bash
# Publish the current ingest source to GitHub.
# The Git working copy is canonical. Never replace it from a historical NAS bundle.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CLONE="$ROOT/work/github/muli-ingest"
cd "$CLONE"
python3 - <<'PY'
import ast, pathlib, sys
root = pathlib.Path('src')
bad = []
for path in sorted(root.rglob('*.py')):
    try:
        ast.parse(path.read_text())
    except SyntaxError as exc:
        bad.append('%s: %s' % (path, exc))
if bad:
    sys.exit('syntax gate failed:\n' + '\n'.join(bad))
print('syntax gate ok')
PY
MSG="${1:-Update ingest service and CLI}"
uv run --locked pytest
uv build
git add src tests docs pyproject.toml uv.lock README.md CHANGELOG.md .github
if git diff --cached --quiet; then echo 'nothing to publish'; exit 0; fi
git commit -m "$MSG"
git push origin main
