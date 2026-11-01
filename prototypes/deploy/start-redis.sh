#!/usr/bin/env bash
# Start a throwaway redis for the RE Manager (port 6379, no persistence).
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
export PROTOTYPE_ROOT=${PROTOTYPE_ROOT:-/tmp/bluesky-adaptive-prototypes}
REDIS_PORT=${REDIS_PORT:-6379}
mkdir -p "$PROTOTYPE_ROOT/redis"

exec pixi run --manifest-path "$HERE/pixi.toml" redis-server \
  --bind 127.0.0.1 --port "$REDIS_PORT" --save "" --appendonly no --dir "$PROTOTYPE_ROOT/redis"
