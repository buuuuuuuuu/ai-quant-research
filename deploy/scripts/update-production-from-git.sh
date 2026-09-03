#!/usr/bin/env bash
set -Eeuo pipefail

source_dir="${1:-/root/code/ai-quant-research}"
branch="${2:-main}"
expected_schema_version="${3:-14}"
remote="${AI_QUANT_GIT_REMOTE:-origin}"
app_dir="${AI_QUANT_APP_DIR:-/opt/ai-quant/app}"
state_dir="${AI_QUANT_STATE_DIR:-/var/lib/ai-quant}"
env_file="${AI_QUANT_ENV_FILE:-/etc/ai-quant/ai-quant.env}"
health_url="${AI_QUANT_HEALTH_URL:-http://127.0.0.1:8080/health}"

timer_units=(
  ai-quant-sync.timer
  ai-quant-backup.timer
  ai-quant-monitor.timer
)
service_units=(
  ai-quant-web.service
  ai-quant-sync.service
  ai-quant-backup.service
  ai-quant-monitor.service
)
production_environment=(
  "AI_QUANT_DATA_DIR=$state_dir/data"
  "AI_QUANT_DUCKDB_PATH=$state_dir/data/catalog.duckdb"
  "AI_QUANT_ARTIFACT_DIR=$state_dir/artifacts"
  "AI_QUANT_BACKUP_DIR=$state_dir/backups"
)

writes_stopped=0
pre_upgrade_backup_id=""

log() {
  printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

stop_writers() {
  systemctl stop "${timer_units[@]}"
  systemctl stop "${service_units[@]}"
  writes_stopped=1
}

on_error() {
  local status=$?
  trap - ERR
  if ((writes_stopped)); then
    systemctl stop "${timer_units[@]}" >/dev/null 2>&1 || true
    systemctl stop "${service_units[@]}" >/dev/null 2>&1 || true
    log "Deployment failed with exit code $status. AI Quant writers remain stopped."
  else
    log "Deployment failed with exit code $status before writers were stopped."
  fi
  if [[ -n "$pre_upgrade_backup_id" ]]; then
    log "Verified pre-upgrade backup: $pre_upgrade_backup_id"
  fi
  log "Inspect the error and logs before starting any AI Quant unit."
  exit "$status"
}
trap on_error ERR

json_field() {
  local field="$1"
  "$app_dir/.venv/bin/python" -c '
import json
import sys

payload = json.load(sys.stdin)
value = payload
for part in sys.argv[1].split("."):
    value = value[part]
print(value)
' "$field"
}

run_ai() {
  (
    # The source checkout normally lives below /root and is intentionally not
    # traversable by the service account. Run the installed CLI from its own
    # production directory so Settings discovery never inspects /root.
    cd "$app_dir"
    runuser -u ai-quant -- env "${production_environment[@]}" \
      "$app_dir/.venv/bin/ai-quant" "$@"
  )
}

create_verified_backup() {
  local label="$1"
  local created backup_id verified verify_status
  log "Creating $label consistent database backup"
  created="$(run_ai ops backup)"
  backup_id="$(json_field backup_id <<<"$created")"
  if [[ -z "$backup_id" ]]; then
    log "Backup command did not return a backup_id"
    return 1
  fi
  verified="$(run_ai ops backup-verify --backup-id "$backup_id")"
  verify_status="$(json_field status <<<"$verified")"
  if [[ "$verify_status" != "verified" ]]; then
    log "Backup verification failed for $backup_id: $verify_status"
    return 1
  fi
  log "Verified $label backup $backup_id"
  printf '%s\n' "$backup_id"
}

if [[ "$(id -u)" != "0" ]]; then
  printf '%s\n' 'This updater must run as root.' >&2
  exit 1
fi
if [[ ! "$expected_schema_version" =~ ^[0-9]+$ ]]; then
  printf 'Expected schema version must be an integer: %s\n' "$expected_schema_version" >&2
  exit 1
fi
for command in git systemctl runuser curl uv; do
  if ! command -v "$command" >/dev/null 2>&1; then
    printf 'Required command is not installed: %s\n' "$command" >&2
    exit 1
  fi
done
if [[ ! -d "$source_dir/.git" ]]; then
  printf 'Source checkout does not exist: %s\n' "$source_dir" >&2
  exit 1
fi
if [[ ! -d "$app_dir/.git" || ! -x "$app_dir/.venv/bin/ai-quant" ]]; then
  printf 'Production application is not installed correctly: %s\n' "$app_dir" >&2
  exit 1
fi
if [[ ! -f "$env_file" ]]; then
  printf 'Production environment file does not exist: %s\n' "$env_file" >&2
  exit 1
fi
if [[ -n "$(git -C "$source_dir" status --porcelain)" ]]; then
  printf 'Source checkout has local changes; deployment aborted: %s\n' "$source_dir" >&2
  git -C "$source_dir" status --short >&2
  exit 1
fi

log "Preflight passed; source=$source_dir branch=$branch app=$app_dir"
old_status="$(run_ai data status)"
old_schema_version="$(json_field schema_version <<<"$old_status")"
if ((old_schema_version > expected_schema_version)); then
  log "Database schema $old_schema_version is newer than requested $expected_schema_version"
  exit 1
fi
log "Current production schema is v$old_schema_version"

log "Stopping Web, synchronization, backup, and monitor writers"
stop_writers
pre_upgrade_backup_id="$(create_verified_backup pre-upgrade)"

log "Fetching $remote/$branch"
git -C "$source_dir" fetch --prune "$remote" "$branch"
git -C "$source_dir" switch "$branch"
git -C "$source_dir" merge --ff-only "$remote/$branch"
source_commit="$(git -C "$source_dir" rev-parse HEAD)"
remote_commit="$(git -C "$source_dir" rev-parse "$remote/$branch")"
if [[ "$source_commit" != "$remote_commit" ]]; then
  log "Local $branch is not exactly $remote/$branch; refusing to deploy"
  exit 1
fi
log "Pinned release commit: $source_commit"

installer="$source_dir/deploy/scripts/install-production.sh"
if [[ ! -x "$installer" && ! -f "$installer" ]]; then
  log "Production installer is missing: $installer"
  exit 1
fi
log "Installing the pinned commit and applying database migrations"
bash "$installer" "$source_dir" "$source_commit"

deployed_commit="$(git -C "$app_dir" rev-parse HEAD)"
if [[ "$deployed_commit" != "$source_commit" ]]; then
  log "Deployed commit $deployed_commit does not match pinned commit $source_commit"
  exit 1
fi

new_status="$(run_ai data status)"
new_schema_version="$(json_field schema_version <<<"$new_status")"
program_schema_version="$(json_field expected_schema_version <<<"$new_status")"
if [[ "$new_schema_version" != "$expected_schema_version" ]]; then
  log "Database schema is $new_schema_version; expected $expected_schema_version"
  exit 1
fi
if [[ "$program_schema_version" != "$expected_schema_version" ]]; then
  log "Program expects schema $program_schema_version; expected $expected_schema_version"
  exit 1
fi
printf '%s' "$new_status" | "$app_dir/.venv/bin/python" -c '
import json
import sys

payload = json.load(sys.stdin)
required = {
    "document_event_extraction",
    "document_event",
    "document_event_evidence",
    "document_event_research_run",
    "stock_pool",
    "stock_pool_member",
    "stock_pool_revision",
    "managed_stock",
}
missing = sorted(required - set(payload.get("relations", {})))
if missing:
    raise SystemExit(f"Required M8/M9 relations are missing: {missing}")
'
log "Schema v$expected_schema_version and M8/M9 relations verified"

systemctl daemon-reload
systemctl enable ai-quant-web.service "${timer_units[@]}"
systemctl start ai-quant-web.service

health_response=""
for _attempt in $(seq 1 30); do
  if health_response="$(curl --fail --silent --show-error "$health_url")"; then
    break
  fi
  sleep 2
done
if [[ -z "$health_response" ]]; then
  log "Web health check did not succeed: $health_url"
  exit 1
fi
log "Web health check passed: $health_response"

post_upgrade_backup_id="$(create_verified_backup post-upgrade)"
systemctl start "${timer_units[@]}"
systemctl --no-pager --full status ai-quant-web.service >/dev/null

writes_stopped=0
trap - ERR
log "Deployment succeeded"
printf 'release_commit=%s\n' "$source_commit"
printf 'schema_version=%s\n' "$new_schema_version"
printf 'pre_upgrade_backup_id=%s\n' "$pre_upgrade_backup_id"
printf 'post_upgrade_backup_id=%s\n' "$post_upgrade_backup_id"
printf 'health_url=%s\n' "$health_url"
