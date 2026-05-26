#!/usr/bin/env bash
set -euo pipefail

ombre_repo_root() {
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  cd "${script_dir}/.." && pwd
}

ombre_compose_file() {
  if [[ -n "${COMPOSE_FILE:-}" ]]; then
    printf '%s\n' "${COMPOSE_FILE}"
    return
  fi
  for candidate in compose.local.yml compose.hk.yml docker-compose.user.yml docker-compose.yml; do
    if [[ -f "${candidate}" ]]; then
      printf '%s\n' "${candidate}"
      return
    fi
  done
  echo "No compose file found. Set COMPOSE_FILE=/path/to/compose.yml" >&2
  exit 1
}

ombre_compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose "$@"
  else
    echo "Docker Compose not found. Install docker compose first." >&2
    exit 1
  fi
}

ombre_default_health_url() {
  local compose_file="${1}"
  case "${compose_file}" in
    *docker-compose.user.yml) printf '%s\n' "http://127.0.0.1:8000/health" ;;
    *) printf '%s\n' "http://127.0.0.1:18001/health" ;;
  esac
}

ombre_wait_for_health() {
  local url="${1}"
  local tries="${2:-30}"
  local delay="${3:-2}"

  if ! command -v curl >/dev/null 2>&1; then
    echo "curl not found; skip health check: ${url}"
    return 0
  fi

  echo "Health check: ${url}"
  for ((i = 1; i <= tries; i++)); do
    if curl -fsS "${url}" >/dev/null; then
      echo "Health check OK"
      return 0
    fi
    sleep "${delay}"
  done

  echo "Health check failed after ${tries} tries: ${url}" >&2
  return 1
}
