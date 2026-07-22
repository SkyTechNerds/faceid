#!/usr/bin/env bash
# Sync the app source into the add-on build context.
# Supervisor builds the add-on from faceid-addon/ only, so app/, static/ and
# requirements.txt must be duplicated there. Run after changing any of them.
set -euo pipefail
cd "$(dirname "$0")/.."
rm -rf faceid-addon/app faceid-addon/static
cp -r app static requirements.txt CHANGELOG.md faceid-addon/
echo "faceid-addon/ synced (incl. CHANGELOG.md)."
echo "Release checklist: bump version in faceid-addon/config.yaml, add a CHANGELOG entry,"
echo "commit, push, then: gh release create v<version> --title v<version> --notes-file <(latest changelog section)"
