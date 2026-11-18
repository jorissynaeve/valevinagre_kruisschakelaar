#!/usr/bin/env bash
# Draait de host-tests van ledscene.h in een gcc-container (Windows heeft hier geen C++-compiler): vectoren, ongeldige payloads, fuzz, onder ASAN en UBSAN.
# Gebruik: protocol/ledscene/tests/run.sh
set -euo pipefail
cd "$(dirname "$0")/.."          # protocol/ledscene
WIN_PATH="$(pwd -W 2>/dev/null || pwd)"
export MSYS_NO_PATHCONV=1

docker run --rm -v "${WIN_PATH}:/w" -w /w gcc:14 bash -c '
set -e
mkdir -p /tmp/b
FLAGS="-std=c++17 -Wall -Wextra -Werror -g -O1 -fsanitize=address,undefined -fno-sanitize-recover=undefined"
g++ $FLAGS tests/test_vectors.cpp -o /tmp/b/test_vectors
# de header moet ook zonder sanitizers en met -O2 -Wpedantic schoon compileren (zoals op de ESP32)
g++ -std=c++17 -O2 -Wall -Wextra -Wpedantic -Werror -fsyntax-only -include ledscene.h -x c++ /dev/null
ASAN_OPTIONS=detect_leaks=1 /tmp/b/test_vectors vectors/vectors.txt
'
