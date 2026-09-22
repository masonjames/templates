#!/usr/bin/env bash
set -euo pipefail

public_config=$(mktemp -d)
cleanup() {
  rm -rf -- "$public_config"
}
trap cleanup EXIT

printf '{}\n' > "$public_config/config.json"
export DOCKER_CONFIG="$public_config"
export DAGGER_NO_NAG=1

exec dagger "$@"
