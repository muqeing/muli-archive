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
MSG="${1:-Update archive console and CLI}"
uv run --locked python -m unittest discover -s tests
uv build
git add src tests docs tools .github pyproject.toml uv.lock README.md CHANGELOG.md
if git diff --cached --quiet; then echo 'nothing to publish'; exit 0; fi
git commit -m "$MSG"
git push origin main
