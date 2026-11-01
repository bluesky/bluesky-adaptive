#!/usr/bin/env bash
# Start the caproto XRT IOC for the BMM beamline (Channel Access on port 5064).
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
TST_SIM_TOOLS=${TST_SIM_TOOLS:-$REPO/tst-sim-tools}
export PROTOTYPE_ROOT=${PROTOTYPE_ROOT:-/tmp/bluesky-adaptive-prototypes}
export EPICS_CA_AUTO_ADDR_LIST=${EPICS_CA_AUTO_ADDR_LIST:-NO}
export EPICS_CA_ADDR_LIST=${EPICS_CA_ADDR_LIST:-127.0.0.1}
export EPICS_CAS_INTF_ADDR_LIST=${EPICS_CAS_INTF_ADDR_LIST:-127.0.0.1}
mkdir -p "$PROTOTYPE_ROOT"

cd "$TST_SIM_TOOLS"
exec pixi run --frozen -e ioc python iocs/xrt_xml_ioc.py --xml iocs/bmm.xml --prefix 'XF:31ID1-XRT{BMM:01}' "$@"
