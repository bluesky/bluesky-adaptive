#!/usr/bin/env bash
# Start the bluesky-queueserver RE Manager (ZMQ control 60615, info 60625) with the
# prototype startup script, in the tst-sim-tools `qs` environment.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
TST_SIM_TOOLS=${TST_SIM_TOOLS:-$REPO/tst-sim-tools}
export PROTOTYPE_ROOT=${PROTOTYPE_ROOT:-/tmp/bluesky-adaptive-prototypes}
export TILED_URI=${TILED_URI:-http://localhost:8000}
export TILED_API_KEY=${TILED_API_KEY:-secret}
export TST_DATA_DIR=${TST_DATA_DIR:-$PROTOTYPE_ROOT/data}
export EPICS_CA_AUTO_ADDR_LIST=${EPICS_CA_AUTO_ADDR_LIST:-NO}
export EPICS_CA_ADDR_LIST=${EPICS_CA_ADDR_LIST:-127.0.0.1}
export PYTHONUNBUFFERED=1
REDIS_PORT=${REDIS_PORT:-6379}
QS_DIR=$PROTOTYPE_ROOT/qs
mkdir -p "$QS_DIR" "$TST_DATA_DIR"
cp "$HERE/user_group_permissions.yaml" "$QS_DIR/user_group_permissions.yaml"

cd "$PROTOTYPE_ROOT"
exec pixi run --frozen --manifest-path "$TST_SIM_TOOLS/pyproject.toml" -e qs start-re-manager \
  --startup-script "$HERE/qs_startup.py" \
  --existing-plans-devices "$QS_DIR/existing_plans_and_devices.yaml" \
  --user-group-permissions "$QS_DIR/user_group_permissions.yaml" \
  --update-existing-plans-devices ENVIRONMENT_OPEN \
  --redis-addr "localhost:$REDIS_PORT" \
  --zmq-control-addr tcp://127.0.0.1:60615 \
  --zmq-info-addr tcp://127.0.0.1:60625 \
  --zmq-publish-console ON \
  --device-max-depth 0 \
  --use-ipython-kernel OFF \
  "$@"
