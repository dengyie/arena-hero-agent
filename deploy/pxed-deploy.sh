#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/arena-hero/arena-hero-agent.git"
APP_DIR="${ARENA_HERO_APP_DIR:-/data/arena-hero-agent}"
ENV_FILE="${ARENA_HERO_ENV_FILE:-$APP_DIR/.env.protected}"
SUPERVISOR_CONF="${ARENA_HERO_SUPERVISOR_CONF:-/personal/pxed/supervisor-arena-hero.conf}"
SUPERVISOR_ROOT="${ARENA_HERO_SUPERVISOR_ROOT:-/personal/pxed/supervisord.conf}"
MODE="dry-run"
COMBAT_MODE="shadow"
SOURCE_DIR=""
PREPARE_ONLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO_URL="$2"; shift 2;;
    --source-dir) SOURCE_DIR="$2"; shift 2;;
    --combat-mode) COMBAT_MODE="$2"; shift 2;;
    --live) MODE="live"; shift;;
    --dry-run) MODE="dry-run"; shift;;
    --prepare-only) PREPARE_ONLY=1; shift;;
    *) echo "unknown argument: $1" >&2; exit 2;;
  esac
done

case "$COMBAT_MODE" in
  current|shadow|production|positioning|live-sweep|live-precision|live-cell|live) ;;
  *) echo "invalid combat mode: $COMBAT_MODE" >&2; exit 2;;
esac

PYTHON_BIN="${ARENA_HERO_PYTHON:-$(command -v python3)}"
[[ -x "$PYTHON_BIN" ]] || { echo "Python executable missing: $PYTHON_BIN" >&2; exit 4; }

sync_source_dir() {
  local source=$1 backup_root backup
  [[ -d "$source/arena_agent" && -d "$source/tests" && -d "$source/deploy" ]] || {
    echo "invalid source directory: $source" >&2
    exit 2
  }
  mkdir -p "$APP_DIR"
  backup_root="${ARENA_HERO_BACKUP_ROOT:-$(dirname "$APP_DIR")/arena-hero-agent-backups}"
  backup="$backup_root/$(date +%Y%m%d%H%M%S)-$$-${COMBAT_MODE}"
  mkdir -p "$backup"
  for path in arena_agent tests docs deploy README.md requirements.txt; do
    [[ -e "$APP_DIR/$path" ]] && cp -a "$APP_DIR/$path" "$backup/"
    rm -rf "$APP_DIR/$path"
    [[ -e "$source/$path" ]] && cp -a "$source/$path" "$APP_DIR/$path"
  done
  [[ -e "$SUPERVISOR_CONF" ]] && cp -a "$SUPERVISOR_CONF" "$backup/supervisor-arena-hero.conf"
  echo "backup=$backup"
}

if [[ -n "$SOURCE_DIR" ]]; then
  sync_source_dir "$SOURCE_DIR"
elif [[ ! -e "$APP_DIR" ]]; then
  git clone "$REPO_URL" "$APP_DIR"
elif [[ -d "$APP_DIR/.git" ]]; then
  git -C "$APP_DIR" fetch --prune origin
  git -C "$APP_DIR" pull --ff-only
else
  echo "$APP_DIR is not a Git checkout; use --source-dir with a verified staging tree" >&2
  exit 2
fi

"$PYTHON_BIN" -c 'import websockets' || {
  echo "websockets is unavailable in $PYTHON_BIN" >&2
  exit 4
}
cd "$APP_DIR"
"$PYTHON_BIN" -m unittest discover -s tests -p 'test_*.py'
"$PYTHON_BIN" -m compileall -q arena_agent
bash -n "$APP_DIR/deploy/pxed-deploy.sh"

if [[ "$MODE" == "live" && "$PREPARE_ONLY" -eq 0 ]]; then
  [[ -s "$ENV_FILE" ]] || { echo "live requested but protected token file is missing: $ENV_FILE" >&2; exit 3; }
  grep -q '^ARENA_HERO_TOKEN=' "$ENV_FILE" || { echo "protected token file has no ARENA_HERO_TOKEN" >&2; exit 3; }
  token=$(awk -F= '$1=="ARENA_HERO_TOKEN" {print substr($0,index($0,"=")+1)}' "$ENV_FILE")
  [[ "$token" != "" && "$token" != "[REDACTED]" ]] || { echo "protected token is empty/redacted" >&2; exit 3; }
fi

mkdir -p "$APP_DIR/runtime" "$(dirname "$SUPERVISOR_CONF")"
if [[ "$MODE" == "live" ]]; then
  sed -e "s|__ARENA_PYTHON_BIN__|$PYTHON_BIN|g" \
      -e "s|__ARENA_COMBAT_MODE__|$COMBAT_MODE|g" \
      "$APP_DIR/deploy/supervisor-arena-hero.conf" > "$SUPERVISOR_CONF"
else
  sed "s|__ARENA_PYTHON_BIN__|$PYTHON_BIN|g" \
      "$APP_DIR/deploy/supervisor-arena-hero-dry-run.conf" > "$SUPERVISOR_CONF"
fi
chmod 600 "$SUPERVISOR_CONF"

if [[ "$PREPARE_ONLY" -eq 1 ]]; then
  echo "prepared app=$APP_DIR mode=$MODE combat_mode=$COMBAT_MODE"
  exit 0
fi

supervisorctl -c "$SUPERVISOR_ROOT" reread
supervisorctl -c "$SUPERVISOR_ROOT" update
supervisorctl -c "$SUPERVISOR_ROOT" restart arena-hero-agent
supervisorctl -c "$SUPERVISOR_ROOT" status arena-hero-agent
