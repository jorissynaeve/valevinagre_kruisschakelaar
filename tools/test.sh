#!/usr/bin/env bash
# Draait de tests in containers (Windows heeft hier geen C++-compiler of vaste Python-omgeving).
#
# Gebruik: tools/test.sh [unit|scen|quick|sweep|probe|all]   C++-kern en simulator (standaard: all)
#          tools/test.sh addon                               Python-add-on + interop met de C++-kern
#          tools/test.sh e2e                                 simulator (UDP) + add-on + MQTT-broker in docker compose
set -euo pipefail
cd "$(dirname "$0")/.."
WIN_PATH="$(pwd -W 2>/dev/null || pwd)"
export MSYS_NO_PATHCONV=1

gcc() { docker run --rm -v "${WIN_PATH}:/w" -w /w gcc:14 bash -c "$1"; }

case "${1:-all}" in
  addon)
    mkdir -p build
    # 1. echte node-frames en hash-gevallen uit de C++-kern
    gcc "make -s build/kkvec && build/kkvec gen" > build/vectors_cpp.txt
    # 2. Python: eigen tests + de vectoren verifiëren, en eigen vectoren schrijven
    docker build -q --target test -t kk-addon-test addon > /dev/null
    docker run --rm -v "${WIN_PATH}/build:/vec" -e KK_VECTORS=/vec kk-addon-test
    # 3. de C++-kern leest wat Python maakte
    gcc "build/kkvec check build/vectors_py.txt"
    ;;
  e2e)
    mkdir -p build
    gcc "make -s build/kkudp"
    rc=0
    docker compose -f e2e/compose.yaml up --build --abort-on-container-exit --exit-code-from e2e || rc=$?
    docker compose -f e2e/compose.yaml down -v > /dev/null 2>&1 || true
    exit $rc
    ;;
  *)
    gcc "make -s build/kktest && build/kktest ${1:-all}"
    ;;
esac
