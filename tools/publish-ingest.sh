#!/usr/bin/env bash
# Publish the current ingest source to GitHub.
# The release workspace holds the source; work/github/muli-ingest is the clone.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BUNDLE="$ROOT/work/ingest-source-release/linux-fixtures/bundle/ingest"
CLONE="$ROOT/work/github/muli-ingest"
BUNDLE="$BUNDLE" python3 -c "
import ast, os, pathlib, sys
root = pathlib.Path(os.environ['BUNDLE']) / 'src'
bad = []
for path in sorted(root.rglob('*.py')):
    try:
        ast.parse(path.read_text())
    except SyntaxError as exc:
        bad.append('%s: %s' % (path, exc))
if bad:
    sys.exit('syntax gate failed:\n' + '\n'.join(bad))
print('syntax gate ok')
"
MSG="$1"
if [ -z "$MSG" ]; then MSG="Update ingest service"; fi
cd "$CLONE"
git pull --ff-only origin main
find src -mindepth 1 -delete
cp -R "$BUNDLE/src/." src/
find tests -mindepth 1 -delete
cp -R "$BUNDLE/tests/." tests/
cp "$BUNDLE/Dockerfile" Dockerfile
cp "$BUNDLE/compose.lan.yaml" compose.lan.yaml
cp "$BUNDLE/compose.yaml" compose.yaml
cp -R "$BUNDLE/docs/." docs/
find . -name '__pycache__' -type d -prune -exec find {} -delete \; 2>/dev/null || true
git add -A
if git diff --cached --quiet; then echo 'nothing to publish'; exit 0; fi
git commit -m "$MSG"
git push origin main
