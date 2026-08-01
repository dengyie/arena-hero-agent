#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/arena-hero/arena-hero-agent.git"
APP_DIR="/data/arena-hero-agent"
ENV_FILE="/data/arena-hero-agent/.env.protected"
SUPERVISOR_CONF="/personal/pxed/supervisor-arena-hero.conf"
MODE="dry-run"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO_URL="$2"; shift 2;;
    --live) MODE="live"; shift;;
    --dry-run) MODE="dry-run"; shift;;
    *) echo "unknown argument: $1" >&2; exit 2;;
  esac
done

if [[ ! -d "$APP_DIR/.git" ]]; then
  git clone "$REPO_URL" "$APP_DIR"
else
  git -C "$APP_DIR" fetch --prune origin
  git -C "$APP_DIR" pull --ff-only
fi
PYTHON_BIN="python3"
if python3 -m venv "$APP_DIR/.venv" 2>/dev/null; then
  PYTHON_BIN="$APP_DIR/.venv/bin/python"
  "$PYTHON_BIN" -m pip install --disable-pip-version-check -r "$APP_DIR/requirements.txt"
else
  echo "venv unavailable; using system Python and existing/user-site dependencies" >&2
  python3 -m pip install --user --disable-pip-version-check -r "$APP_DIR/requirements.txt" || true
fi
cd "$APP_DIR"
"$PYTHON_BIN" -m unittest discover -s tests -p 'test_*.py'

if [[ "$MODE" == "live" ]]; then
  [[ -s "$ENV_FILE" ]] || { echo "live requested but protected token file is missing: $ENV_FILE" >&2; exit 3; }
  grep -q '^ARENA_HERO_TOKEN=' "$ENV_FILE" || { echo "protected token file has no ARENA_HERO_TOKEN" >&2; exit 3; }
  token=$(awk -F= '$1=="ARENA_HERO_TOKEN" {print substr($0,index($0,"=")+1)}' "$ENV_FILE")
  [[ "$token" != "" && "$token" != "[REDACTED]" ]] || { echo "protected token is empty/redacted" >&2; exit 3; }
fi

mkdir -p "$APP_DIR/runtime"
if [[ "$MODE" == "live" ]]; then
  sed "s|/data/arena-hero-agent/.venv/bin/python|$PYTHON_BIN|g" "$APP_DIR/deploy/supervisor-arena-hero.conf" > "$SUPERVISOR_CONF"
else
  sed "s|/data/arena-hero-agent/.venv/bin/python|$PYTHON_BIN|g" "$APP_DIR/deploy/supervisor-arena-hero-dry-run.conf" > "$SUPERVISOR_CONF"
fi
chmod 600 "$SUPERVISOR_CONF"
supervisorctl -c /personal/pxed/supervisord.conf reread
supervisorctl -c /personal/pxed/supervisord.conf update
supervisorctl -c /personal/pxed/supervisord.conf restart arena-hero-agent
supervisorctl -c /personal/pxed/supervisord.conf status arena-hero-agent
