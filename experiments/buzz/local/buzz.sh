#!/usr/bin/env bash
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export DOCKER_CONTEXT=colima-buzz

compose() {
  docker-compose --project-directory "$project_dir" \
    --file "$project_dir/compose.yml" --env-file "$project_dir/.env" "$@"
}

case "${1:-help}" in
  start)
    if ! colima status buzz >/dev/null 2>&1; then
      colima start buzz --cpu 4 --memory 6 --disk 40 --mount none --ssh-config=false --activate=false
    fi
    compose up -d --wait --wait-timeout 180 --pull never
    printf '\nBuzz relay: http://localhost:3000\nConnect Buzz Local to ws://localhost:3000.\n'
    ;;
  stop)
    compose stop
    ;;
  status)
    colima status buzz
    compose ps
    ;;
  logs)
    shift
    compose logs --tail=100 "${@:-relay}"
    ;;
  vm-stop)
    compose stop
    colima stop buzz
    ;;
  *)
    printf 'Usage: %s {start|stop|status|logs [service]|vm-stop}\n' "$0"
    printf 'stop preserves all data; vm-stop also releases VM memory.\n'
    ;;
esac
