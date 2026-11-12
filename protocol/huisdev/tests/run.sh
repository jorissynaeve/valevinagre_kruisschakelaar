#!/usr/bin/env bash
# Draait de host-tests van huisdev.h in een gcc-container (Windows heeft hier geen C++-compiler; de image heeft ook Python).
# Gebruik: protocol/huisdev/tests/run.sh [build|test|all]    (standaard: all)
set -euo pipefail
cd "$(dirname "$0")/.."          # protocol/huisdev
WIN_PATH="$(pwd -W 2>/dev/null || pwd)"
export MSYS_NO_PATHCONV=1

in_gcc() { docker run --rm -v "${WIN_PATH}:/w" -w /w gcc:14 bash -c "$1"; }

BUILD='mkdir -p build tests/fixtures
FLAGS="-std=gnu++17 -DESP32 -Wall -Wextra -g -fsanitize=address,undefined -fno-sanitize-recover=undefined -Itests/shim -I."
g++ $FLAGS tests/driver.cpp -o build/driver
g++ $FLAGS -DHUISDEV_NO_WEBSERVER tests/nows.cpp -o build/nows && build/nows && echo "nows: ok (HUISDEV_NO_WEBSERVER compileert zonder WebServer.h)"'

TEST='ASAN_OPTIONS=detect_leaks=1 HUISDEV_DRIVER=build/driver python3 -m unittest discover -s tests -p "test_*.py" -v 2>&1'

case "${1:-all}" in
  build) in_gcc "$BUILD" ;;
  test)  in_gcc "$TEST" ;;
  all)   in_gcc "$BUILD && $TEST" ;;
  *) echo "gebruik: $0 [build|test|all]"; exit 2 ;;
esac
