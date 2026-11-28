#!/usr/bin/env bash
set -euo pipefail

if [[ "${GITHUB_ACTIONS:-}" != "true" ]]; then
  echo "ERROR: Stale benchmark-server cleanup is only allowed on CI runners." >&2
  exit 1
fi

port_containers() {
  local ids
  ids="$(docker ps -q)" || return 1
  [[ -n "$ids" ]] || return 0
  docker inspect $ids | jq -r '
    .[] | select(any(.NetworkSettings.Ports[]?[]?; .HostPort == "9000")) |
    [.Id, (.Config.Labels["com.docker.compose.service"] // ""), .Name] | @tsv'
}

# A runner executes one benchmark job at a time; remove only its old server.
owners="$(port_containers)"
while IFS=$'\t' read -r id service name; do
  [[ -n "$id" ]] || continue
  if [[ "$service" != "benchmark-server" ]]; then
    echo "ERROR: TCP port 9000 belongs to non-benchmark container $name ($id); leaving it running." >&2
    exit 1
  fi
done <<< "$owners"
while IFS=$'\t' read -r id service name; do
  [[ -n "$id" ]] || continue
  echo "Removing stale benchmark-server container $name ($id) on TCP port 9000."
  docker rm -f "$id"
done <<< "$owners"

listeners="$(ss -H -ltn 'sport = :9000')"
if [[ -n "$listeners" ]]; then
  owners="$(port_containers)"
  if [[ -n "$owners" ]]; then
    echo "ERROR: A container still publishes TCP port 9000 after cleanup." >&2
    exit 1
  fi
  # A cancelled Docker start can leave a proxy with no owning container.
  listeners="$(sudo -n ss -H -ltnp 'sport = :9000')"
  if [[ -z "$listeners" ]]; then
    echo "TCP port 9000 is clear."
    exit 0
  fi
  if ! pids="$(grep -oE 'pid=[0-9]+' <<< "$listeners" | cut -d= -f2 | sort -u)"; then
    echo "ERROR: Cannot identify the remaining TCP port 9000 listener: $listeners" >&2
    exit 1
  fi
  for pid in $pids; do
    executable="$(sudo -n readlink "/proc/$pid/exe")"
    if [[ "${executable##*/}" != "docker-proxy" ]]; then
      echo "ERROR: TCP port 9000 belongs to $executable (PID $pid); leaving it running." >&2
      exit 1
    fi
  done
  for pid in $pids; do
    echo "Stopping orphan Docker proxy PID $pid on TCP port 9000."
    sudo -n kill -TERM -- "$pid"
  done
fi

for attempt in {1..10}; do
  listeners="$(ss -H -ltn 'sport = :9000')"
  if [[ -z "$listeners" ]]; then
    echo "TCP port 9000 is clear."
    exit 0
  fi
  sleep 1
done
echo "ERROR: TCP port 9000 is still listening after cleanup." >&2
exit 1
