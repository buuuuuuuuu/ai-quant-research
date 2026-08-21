#!/usr/bin/env bash
set -euo pipefail

source_dir="${1:-/root/code/ai-quant-research}"
source_ref="${2:-HEAD}"
app_dir="/opt/ai-quant/app"
state_dir="/var/lib/ai-quant"
env_file="/etc/ai-quant/ai-quant.env"

if [[ "$(id -u)" != "0" ]]; then
  printf '%s\n' 'This installer must run as root.' >&2
  exit 1
fi

if [[ ! -d "$source_dir/.git" ]]; then
  printf 'Source checkout does not exist: %s\n' "$source_dir" >&2
  exit 1
fi

source_commit="$(git -C "$source_dir" rev-parse --verify "$source_ref^{commit}")"
if ! getent group ai-quant >/dev/null; then
  groupadd --system ai-quant
fi
if ! id ai-quant >/dev/null 2>&1; then
  useradd --system --gid ai-quant --home-dir "$state_dir" \
    --shell /usr/sbin/nologin ai-quant
fi

install -d -m 0755 /opt/ai-quant
if [[ ! -d "$app_dir/.git" ]]; then
  git clone --no-hardlinks "$source_dir" "$app_dir"
else
  if [[ -n "$(git -C "$app_dir" status --porcelain)" ]]; then
    printf 'Deployment checkout has local changes: %s\n' "$app_dir" >&2
    exit 1
  fi
  git -C "$app_dir" fetch "$source_dir" "$source_commit"
fi
git -C "$app_dir" checkout --detach "$source_commit"

install -d -o ai-quant -g ai-quant -m 0750 \
  "$state_dir" "$state_dir/data" "$state_dir/artifacts" "$state_dir/backups"

source_data_dir=""
if [[ -f "$source_dir/data/data/catalog.duckdb" ]]; then
  source_data_dir="$source_dir/data/data"
elif [[ -f "$source_dir/data/catalog.duckdb" ]]; then
  source_data_dir="$source_dir/data"
fi

if [[ ! -f "$state_dir/data/catalog.duckdb" ]]; then
  if [[ -n "$source_data_dir" ]]; then
    cp -a "$source_data_dir/." "$state_dir/data/"
    printf 'Copied existing research data from %s\n' "$source_data_dir"
  fi
fi

if [[ -n "$source_data_dir" && -d "$source_data_dir/backups" ]]; then
  for backup_file in "$source_data_dir"/backups/*.duckdb; do
    if [[ -f "$backup_file" && ! -L "$backup_file" ]]; then
      destination="$state_dir/backups/${backup_file##*/}"
      if [[ ! -e "$destination" ]]; then
        cp -a "$backup_file" "$destination"
      fi
    fi
  done
fi

if [[ -d "$source_dir/artifacts" && -z "$(find "$state_dir/artifacts" -mindepth 1 -print -quit)" ]]; then
  cp -a "$source_dir/artifacts/." "$state_dir/artifacts/"
fi
chown -R ai-quant:ai-quant "$state_dir/data" "$state_dir/artifacts" "$state_dir/backups"

if [[ -x /usr/bin/python3.12 ]]; then
  (cd "$app_dir" && uv sync --extra dev --python /usr/bin/python3.12)
else
  (cd "$app_dir" && uv sync --extra dev)
fi
chmod o+rx /opt/ai-quant "$app_dir"
resolved_python="$(readlink -f "$app_dir/.venv/bin/python")"
if [[ "$resolved_python" == /root/* || "$resolved_python" == /home/* ]]; then
  printf 'Production Python cannot be inside a protected home directory: %s\n' \
    "$resolved_python" >&2
  printf '%s\n' 'Install a system Python such as /usr/bin/python3.12 and rerun the installer.' >&2
  exit 1
fi
if ! (cd "$app_dir" && runuser -u ai-quant -- "$app_dir/.venv/bin/ai-quant" --help >/dev/null); then
  printf '%s\n' 'The ai-quant service account cannot execute the production virtual environment.' >&2
  exit 1
fi

production_environment=(
  "AI_QUANT_DATA_DIR=$state_dir/data"
  "AI_QUANT_DUCKDB_PATH=$state_dir/data/catalog.duckdb"
  "AI_QUANT_ARTIFACT_DIR=$state_dir/artifacts"
  "AI_QUANT_BACKUP_DIR=$state_dir/backups"
)
(
  cd "$app_dir"
  runuser -u ai-quant -- env "${production_environment[@]}" \
    "$app_dir/.venv/bin/ai-quant" data init >/dev/null
  if [[ -n "$source_data_dir" ]]; then
    runuser -u ai-quant -- env "${production_environment[@]}" \
      "$app_dir/.venv/bin/ai-quant" ops backup-relocate --old-data-dir "$source_data_dir"
  fi
)

install -d -m 0700 /etc/ai-quant
if [[ ! -f "$env_file" ]]; then
  install -m 0600 "$app_dir/deploy/systemd/ai-quant.env.example" "$env_file"
else
  for setting in \
    'AI_QUANT_DATA_DIR=/var/lib/ai-quant/data' \
    'AI_QUANT_DUCKDB_PATH=/var/lib/ai-quant/data/catalog.duckdb' \
    'AI_QUANT_ARTIFACT_DIR=/var/lib/ai-quant/artifacts' \
    'AI_QUANT_BACKUP_DIR=/var/lib/ai-quant/backups'; do
    key="${setting%%=*}"
    if ! grep -q "^${key}=" "$env_file"; then
      printf '%s\n' "$setting" >> "$env_file"
    fi
  done
fi

install -m 0644 "$app_dir"/deploy/systemd/*.service \
  "$app_dir"/deploy/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload

printf '%s\n' 'Review /etc/ai-quant/ai-quant.env and configure the login password and secret.'
printf '%s\n' 'Then enable ai-quant-web.service and the sync, backup, and monitor timers.'
