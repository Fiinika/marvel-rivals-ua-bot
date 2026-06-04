#!/usr/bin/env bash
# Remote rollout for the Marvel Rivals UA submission bot. Streamed to the server
# over SSH by .github/workflows/deploy.yml and executed with `bash -s` — it is
# never stored on the server.
#
# All inputs arrive as environment variables, prepended to this script as
# shell-quoted `export` lines on stdin (so nothing sensitive touches argv/ps):
#   Required: BOT_IMAGE, REGISTRY_USER, REGISTRY_TOKEN, DEPLOY_DIR
#   Bot config (any non-empty value is written to .env.prod): BOT_TOKEN,
#     ADMIN_CHAT_ID, PUBLISH_CHAT_ID, ADMIN_USER_IDS, GEMINI_API_KEY, and the
#     optional GEMINI_MODEL / OFFICIAL_NEWS_URL / NEWS_CHECK_INTERVAL_MINUTES /
#     ARTICLE_TIMEZONE / SUBMISSION_COOLDOWN_SECONDS.
#
# Keys we are NOT given are left untouched in .env.prod, so you may also manage
# .env.prod by hand on the box. .env.prod is kept mode 0600. The bot has no HTTP
# endpoint, so instead of an HTTP health check we confirm the container reaches
# a STABLE running state — it must stay `running` with no new restarts for a
# settle window that comfortably exceeds startup, catching slow crash-loops
# (e.g. a bad token that only fails after warm-up), not just instant exits.
set -euo pipefail
umask 077  # files we create (.env.prod) must not be world-readable

: "${BOT_IMAGE:?BOT_IMAGE is required}"
: "${REGISTRY_USER:?REGISTRY_USER is required}"
: "${REGISTRY_TOKEN:?REGISTRY_TOKEN is required}"
: "${DEPLOY_DIR:?DEPLOY_DIR is required}"

COMPOSE_FILE="docker-compose.prod.yml"
ENV_FILE=".env.prod"
SERVICE="bot"
SETTLE_SECONDS=40
POLL_INTERVAL=2

cd "$DEPLOY_DIR"

dc() { docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"; }

# --- Runtime config -------------------------------------------------------
# Upsert KEY=VALUE into .env.prod for each non-empty value; preserve keys we
# were not given. BOT_IMAGE is recorded too so manual `docker compose` commands
# work without re-exporting it.
touch "$ENV_FILE"
chmod 600 "$ENV_FILE"

# Write a value for the docker-compose dotenv parser (NOT a shell). Single-
# quoting makes the value fully literal, so spaces, '#', '$' and backslashes are
# taken verbatim. compose's single-quoted form has no escape for an embedded
# single quote or newline, so refuse those rather than silently corrupt the
# file (none of the bot's tokens/ids/urls contain them).
upsert() {
  local key="$1" val="${2-}"
  [ -n "$val" ] || return 0
  case "$val" in
    *$'\n'*) echo "Refusing to write $key: value contains a newline." >&2; exit 1 ;;
    *\'*)    echo "Refusing to write $key: value contains a single quote." >&2; exit 1 ;;
  esac
  local tmp="${ENV_FILE}.tmp"
  grep -v "^${key}=" "$ENV_FILE" > "$tmp" 2>/dev/null || true
  printf "%s='%s'\n" "$key" "$val" >> "$tmp"
  mv "$tmp" "$ENV_FILE"
  chmod 600 "$ENV_FILE"  # mv inherited the temp file's mode; re-assert 0600
}

upsert BOT_IMAGE "$BOT_IMAGE"
upsert BOT_TOKEN "${BOT_TOKEN-}"
upsert ADMIN_CHAT_ID "${ADMIN_CHAT_ID-}"
upsert PUBLISH_CHAT_ID "${PUBLISH_CHAT_ID-}"
upsert ADMIN_USER_IDS "${ADMIN_USER_IDS-}"
upsert SUBMISSION_COOLDOWN_SECONDS "${SUBMISSION_COOLDOWN_SECONDS-}"
upsert GEMINI_API_KEY "${GEMINI_API_KEY-}"
upsert GEMINI_MODEL "${GEMINI_MODEL-}"
upsert OFFICIAL_NEWS_URL "${OFFICIAL_NEWS_URL-}"
upsert NEWS_CHECK_INTERVAL_MINUTES "${NEWS_CHECK_INTERVAL_MINUTES-}"
upsert ARTICLE_TIMEZONE "${ARTICLE_TIMEZONE-}"

# --- Pull & roll out ------------------------------------------------------
export BOT_IMAGE
echo "$REGISTRY_TOKEN" | docker login ghcr.io -u "$REGISTRY_USER" --password-stdin
# Always drop the GHCR credential on the way out, however we exit.
trap 'docker logout ghcr.io >/dev/null 2>&1 || true' EXIT
dc pull
dc up -d --remove-orphans

# --- Verify ---------------------------------------------------------------
cid="$(dc ps -q "$SERVICE" | tail -n1)"
if [ -z "$cid" ]; then
  echo "Deploy FAILED: service '$SERVICE' has no container after 'up'." >&2
  exit 1
fi

inspect() { docker inspect -f "$1" "$cid" 2>/dev/null; }
baseline="$(inspect '{{.RestartCount}}')" || baseline=0
status=""
restarts="$baseline"
stable=1
elapsed=0
# Poll the whole settle window; fail the moment it leaves `running` or restarts.
while [ "$elapsed" -lt "$SETTLE_SECONDS" ]; do
  sleep "$POLL_INTERVAL"
  elapsed=$((elapsed + POLL_INTERVAL))
  status="$(inspect '{{.State.Status}}')" || status="unknown"
  restarts="$(inspect '{{.RestartCount}}')" || restarts="-1"
  if [ "$status" != "running" ] || [ "$restarts" != "$baseline" ]; then
    stable=0
    break
  fi
done

if [ "$stable" = 1 ]; then
  echo "Deploy OK: $SERVICE stable for ${SETTLE_SECONDS}s ($BOT_IMAGE), restarts=$baseline."
  docker image prune -f >/dev/null 2>&1 || true
else
  echo "Deploy FAILED: $SERVICE unstable (status=$status, restarts ${baseline}->${restarts}, crash-looping?). Recent logs:" >&2
  dc logs --tail 120 "$SERVICE" || true
  exit 1
fi
