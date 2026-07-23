#!/usr/bin/env bash
# Sync the app source into the add-on build context.
# Supervisor builds the add-on from faceid-addon/ only, so app/, static/ and
# requirements.txt must be duplicated there. Run after changing any of them.
set -euo pipefail
cd "$(dirname "$0")/.."
# Import-Konsistenz: jeder "from .x import y" muss im Zielmodul existieren (Lehre aus Issue #1)
python3 - << 'PYCHECK'
import ast, sys
from pathlib import Path
defined = {}
for f in Path("app").glob("*.py"):
    tree = ast.parse(f.read_text())
    defined[f.stem] = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.ClassDef))} | \
                      {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
errors = []
for f in Path("app").glob("*.py"):
    for n in ast.walk(ast.parse(f.read_text())):
        if isinstance(n, ast.ImportFrom) and n.level == 1 and n.module in defined:
            errors += [f"{f.name}: imports {a.name} from {n.module} - not defined there"
                       for a in n.names if a.name not in defined[n.module]]
if errors:
    print("IMPORT CHECK FAILED:"); [print(" ", e) for e in errors]; sys.exit(1)
PYCHECK

rm -rf faceid-addon/app faceid-addon/static
cp -r app static requirements.txt CHANGELOG.md faceid-addon/
echo "faceid-addon/ synced (incl. CHANGELOG.md)."
echo "Release checklist: bump version in faceid-addon/config.yaml, add a CHANGELOG entry,"
echo "commit, push, then: gh release create v<version> --title v<version> --notes-file <(latest changelog section)"
