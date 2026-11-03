#!/usr/bin/env bash
set -euo pipefail
BUNDLE="${1:?usage: install.sh BUNDLE_DIR [INSTALL_DIR]}"
INSTALL="${2:-$BUNDLE/install}"
BUNDLE="$(cd "$BUNDLE" && pwd)"
mkdir -p "$INSTALL"
INSTALL="$(cd "$INSTALL" && pwd)"
python3 "$BUNDLE/scripts/verify_bundle.py" "$BUNDLE"
if [ ! -f "$BUNDLE/source/agentforge-source.tar" ]; then
  echo "missing source archive" >&2; exit 1
fi
mkdir -p "$INSTALL/src"
tar -xf "$BUNDLE/source/agentforge-source.tar" -C "$INSTALL/src"
python3 -m venv "$INSTALL/venv"
"$INSTALL/venv/bin/pip" install --no-index --find-links "$BUNDLE/wheels" -r "$INSTALL/src/backend/requirements.txt"
if [ ! -f "$INSTALL/src/.env" ]; then
  SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
  printf 'AGENTFORGE_SECRET_KEY=%s\nAGENTFORGE_AUTH_ENABLED=false\nAGENTFORGE_LLM_PROVIDER=mock\n' "$SECRET" > "$INSTALL/src/.env"
  chmod 600 "$INSTALL/src/.env"
fi
(
  cd "$INSTALL/src/frontend"
  "$INSTALL/venv/bin/python" -c 'import shutil; assert shutil.which("npm"), "npm is required"'
  npm ci --offline --cache "$BUNDLE/npm-cache"
)
if [ -f "$BUNDLE/images/agentforge-images.tar" ]; then
  docker load -i "$BUNDLE/images/agentforge-images.tar"
fi
(
  cd "$INSTALL/src"
  docker compose -f docker-compose.yml -f docker-compose.offline.yml up --no-build -d
)
echo "Stack started. Run python3 scripts/validation/verify_stack.py from the source directory."
