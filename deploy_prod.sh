#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

for arg in "$@"; do
  case "$arg" in
    -h|--help)
      cat <<'EOF'
Usage: ./deploy_prod.sh

Deploy vitaliy_bot to production via SSH private key.
Configuration is loaded from local .env.deploy or environment variables.

Required configuration:
  PROD_HOST        Production server hostname or IP address
  PROD_USER        SSH username (e.g. deploy-user)
  PROD_SSH_KEY     Path to SSH private key (e.g. ~/.ssh/deploy_key)
  PROD_REMOTE_DIR  Remote application directory (e.g. /path/to/vitaliy_bot)
  PROD_PM2_NAME    Remote PM2 process name (e.g. vitaliy_bot)
  PROD_PYTHON_BIN  Remote Python interpreter (e.g. /path/to/venv/bin/python)

Optional configuration:
  PROD_DEPLOY_CONFIG  Path to deployment environment file (default: .env.deploy)
  PROD_BLOCKED_HOSTS  Comma-separated list of blocked hosts for safety
  PROD_ALLOW_BLOCKED_HOST  Set to 1 to override blocked host check

Template:
  See .env.deploy.example for a configuration template.
EOF
      exit 0
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      echo "Run ./deploy_prod.sh --help for usage." >&2
      exit 1
      ;;
  esac
done

DEPLOY_CONFIG="${PROD_DEPLOY_CONFIG:-$ROOT_DIR/.env.deploy}"
if [[ -f "$DEPLOY_CONFIG" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$DEPLOY_CONFIG"
  set +a
fi

HOST="${PROD_HOST:-}"
USER_NAME="${PROD_USER:-}"
SSH_KEY="${PROD_SSH_KEY:-}"
REMOTE_DIR="${PROD_REMOTE_DIR:-}"
PM2_NAME="${PROD_PM2_NAME:-}"
PYTHON_BIN="${PROD_PYTHON_BIN:-}"

if [[ -z "$HOST" ]]; then
  echo "Error: PROD_HOST is required. Configure it in .env.deploy or the environment." >&2
  exit 2
fi

if [[ ",${PROD_BLOCKED_HOSTS:-}," == *",${HOST},"* && "${PROD_ALLOW_BLOCKED_HOST:-0}" != "1" ]]; then
  echo "Error: Refusing to deploy to blocked host $HOST." >&2
  exit 2
fi

if [[ -z "$USER_NAME" ]]; then
  echo "Error: PROD_USER is required. Configure it in .env.deploy or the environment." >&2
  exit 2
fi

if [[ -z "$SSH_KEY" ]]; then
  echo "Error: PROD_SSH_KEY is required. Configure it in .env.deploy or the environment." >&2
  exit 2
fi

# Expand tilde in key path if present
SSH_KEY="${SSH_KEY/#\~/$HOME}"

if [[ ! -f "$SSH_KEY" ]]; then
  echo "Error: SSH private key not found at '$SSH_KEY'. Verify PROD_SSH_KEY." >&2
  exit 2
fi

if [[ -z "$REMOTE_DIR" ]]; then
  echo "Error: PROD_REMOTE_DIR is required. Configure it in .env.deploy or the environment." >&2
  exit 2
fi

if [[ -z "$PM2_NAME" ]]; then
  echo "Error: PROD_PM2_NAME is required. Configure it in .env.deploy or the environment." >&2
  exit 2
fi

if [[ -z "$PYTHON_BIN" ]]; then
  echo "Error: PROD_PYTHON_BIN is required. Configure it in .env.deploy or the environment." >&2
  exit 2
fi

SSH_OPTIONS=(-o StrictHostKeyChecking=yes -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=4 -i "$SSH_KEY")

run_ssh() {
  ssh "${SSH_OPTIONS[@]}" "${USER_NAME}@${HOST}" "$@"
}

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "Refusing to deploy a dirty worktree." >&2
  exit 2
fi

REVISION="$(git rev-parse HEAD)"
ARCHIVE="$(mktemp -t vitaliy-release.XXXXXX.tar.gz)"
trap 'rm -f "$ARCHIVE"' EXIT

RUNTIME_FILES=()
while IFS= read -r file; do
  [[ "$file" == */* ]] && continue
  RUNTIME_FILES+=("$file")
done < <(git ls-files '*.py' requirements.txt)
if [[ ${#RUNTIME_FILES[@]} -eq 0 ]]; then
  echo "No tracked runtime files found." >&2
  exit 2
fi
git archive --format=tar.gz --output="$ARCHIVE" HEAD "${RUNTIME_FILES[@]}"

REMOTE_ARCHIVE="/tmp/vitaliy-release-${REVISION}.tar.gz"
echo "[1/4] Uploading revision ${REVISION} ($(du -h "$ARCHIVE" | awk '{print $1}'))..."
uploaded=false
for attempt in 1 2 3; do
  if run_ssh "cat > '${REMOTE_ARCHIVE}'" < "$ARCHIVE"; then
    uploaded=true
    break
  fi
  echo "Upload attempt ${attempt} failed; retrying..." >&2
  sleep 2
done
if [[ "$uploaded" != true ]]; then
  echo "Upload failed after 3 attempts. Check host connectivity and SSH key." >&2
  exit 1
fi

echo "[2/4] Validating release in staging..."
run_ssh bash -s -- \
  "$REMOTE_DIR" "$REMOTE_ARCHIVE" "$REVISION" "$PM2_NAME" "$PYTHON_BIN" <<'REMOTE'
set -euo pipefail
remote_dir="$1"
archive="$2"
revision="$3"
pm2_name="$4"
python_bin="$5"
stamp="$(date +%Y%m%d_%H%M%S)"
stage="${remote_dir}/.deploy_staging/${revision}"
backup="${remote_dir}/backups/release_${revision:0:12}_${stamp}"
new_files="${backup}/new_files.txt"
old_revision=""

rm -rf "$stage"
mkdir -p "$stage" "$backup"
tar -xzf "$archive" -C "$stage"
rm -f "$archive"

for source in "$stage"/*.py "$stage"/requirements.txt; do
  [[ -e "$source" ]] || continue
  name="$(basename "$source")"
  if [[ -f "${remote_dir}/${name}" ]]; then
    cp -p "${remote_dir}/${name}" "$backup/"
  else
    printf '%s\n' "$name" >> "$new_files"
  fi
done
if [[ -f "${remote_dir}/database.db" ]]; then
  echo "Creating consistent online SQLite backup and verifying integrity..."
  "$python_bin" -c '
import sys, sqlite3
src_path, dst_path = sys.argv[1], sys.argv[2]
src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True, timeout=30.0)
try:
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst, pages=100)
    finally:
        dst.close()
finally:
    src.close()

chk = sqlite3.connect(dst_path)
try:
    res = chk.execute("PRAGMA integrity_check;").fetchall()
    if res != [("ok",)]:
        raise RuntimeError(f"Backup integrity check failed: {res}")
finally:
    chk.close()
' "${remote_dir}/database.db" "$backup/database.db"
fi
[[ -f "${remote_dir}/REVISION" ]] && old_revision="$(cat "${remote_dir}/REVISION")"

[[ -f "${remote_dir}/.env" ]] && ln -s "${remote_dir}/.env" "$stage/.env"
[[ -f "${remote_dir}/database.db" ]] && ln -s "${remote_dir}/database.db" "$stage/database.db"
(cd "$stage" && "$python_bin" -m py_compile ./*.py && PYTHONDONTWRITEBYTECODE=1 "$python_bin" -c 'import main')

if ! "$python_bin" "$stage/deploy_telegram_healthcheck.py" \
  --pm2-name "$pm2_name" \
  --app-dir "$stage"; then
  echo "Telegram preflight failed; production was not changed." >&2
  rm -rf "$stage"
  exit 1
fi

rollback() {
  rc=$?
  trap - ERR
  echo "Deploy failed; rolling back application code..." >&2
  pm2 stop "$pm2_name" >/dev/null 2>&1 || true
  for saved in "$backup"/*.py "$backup"/requirements.txt; do
    [[ -e "$saved" ]] || continue
    cp -p "$saved" "$remote_dir/"
  done
  if [[ -f "$new_files" ]]; then
    while IFS= read -r name; do
      [[ -n "$name" ]] && rm -f "${remote_dir}/${name}"
    done < "$new_files"
  fi
  if [[ -n "$old_revision" ]]; then
    printf '%s\n' "$old_revision" > "${remote_dir}/REVISION"
  else
    rm -f "${remote_dir}/REVISION"
  fi
  echo "Application code restored to previous revision (${old_revision:-unknown})." >&2
  echo "SAFETY: Production database was NOT reverted automatically to prevent destroying newer user transactions." >&2
  echo "Verified pre-deploy backup is preserved at: ${backup}/database.db" >&2
  echo "If database restoration is required, execute explicit manual recovery." >&2
  pm2 restart "$pm2_name" --update-env >/dev/null 2>&1 || true
  rm -rf "$stage"
  exit "$rc"
}
trap rollback ERR

echo "[3/4] Applying release and migrating database..."
for source in "$stage"/*.py "$stage"/requirements.txt; do
  [[ -e "$source" ]] || continue
  cp -p "$source" "$remote_dir/"
done
pm2 stop "$pm2_name" >/dev/null
(cd "$remote_dir" && "$python_bin" -c '
import asyncio
from database import engine, init_db

async def migrate():
    try:
        await init_db()
    finally:
        await engine.dispose()

asyncio.run(migrate())
')
printf '%s\n' "$revision" > "${remote_dir}/REVISION"

echo "[4/4] Restarting and checking ${pm2_name}..."
pm2 restart "$pm2_name" --update-env >/dev/null
sleep 4
status="$(pm2 jlist | "$python_bin" -c 'import json,sys; name=sys.argv[1]; rows=json.load(sys.stdin); print(next((p["pm2_env"]["status"] for p in rows if p["name"] == name), "missing"))' "$pm2_name")"
if [[ "$status" != "online" ]]; then
  echo "PM2 health check failed: ${pm2_name} is ${status}" >&2
  false
fi

"$python_bin" "${remote_dir}/deploy_telegram_healthcheck.py" \
  --pm2-name "$pm2_name" \
  --app-dir "$remote_dir"

trap - ERR
rm -rf "$stage"
echo "Deployed ${revision}; ${pm2_name}=${status}; backup=${backup}"
REMOTE

echo "Deployment complete."
