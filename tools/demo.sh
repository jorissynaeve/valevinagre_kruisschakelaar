#!/usr/bin/env bash
# Start een gesimuleerd huis (6 nodes), de add-on en een MQTT-broker, en toont de web-UI op http://localhost:8099
# Gebruik: tools/demo.sh [up|down|logs]      Knoppen indrukken: python tools/simctl.py "press 1 0"
set -euo pipefail
cd "$(dirname "$0")/.."
WIN_PATH="$(pwd -W 2>/dev/null || pwd)"
export MSYS_NO_PATHCONV=1
C="docker compose -f e2e/compose.yaml -f e2e/demo.yaml"
case "${1:-up}" in
  up)
    mkdir -p build
    docker run --rm -v "${WIN_PATH}:/w" -w /w gcc:14 make -s build/kkudp
    $C up --build -d mosquitto sim fakeha addon
    echo; echo "Web-UI:  http://localhost:8099"
    echo "Knop:    python tools/simctl.py \"press 1 0\"     (node 1, knop 0)"
    echo "Stand:   python tools/simctl.py \"state 2\""
    echo "Stoppen: tools/demo.sh down"
    ;;
  down) $C down -v ;;
  logs) $C logs -f --tail 50 addon sim ;;
esac
