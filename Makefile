# Host-tests voor de kern. Normaal via tools/test.sh (docker, gcc), op Linux/macOS ook direct met `make test`.
CXX ?= g++
CC ?= gcc
CXXFLAGS ?= -std=c++17 -O2 -g -Wall -Wextra -Wno-unused-parameter
INC = -Icore/include -Ithird_party/monocypher
BUILD = build

CORE_SRC = core/src/frame.cpp core/src/node.cpp
SIM_SRC = sim/test_main.cpp sim/test_unit.cpp sim/test_scenarios.cpp

$(BUILD)/monocypher.o: third_party/monocypher/monocypher.c
	@mkdir -p $(BUILD)
	$(CC) -O2 -c $< -o $@ -Ithird_party/monocypher

$(BUILD)/kktest: $(CORE_SRC) $(SIM_SRC) $(BUILD)/monocypher.o $(wildcard core/include/kk/*.h) sim/sim.h sim/check.h
	@mkdir -p $(BUILD)
	$(CXX) $(CXXFLAGS) $(INC) $(CORE_SRC) $(SIM_SRC) $(BUILD)/monocypher.o -o $@

$(BUILD)/kkvec: $(CORE_SRC) sim/vectors.cpp $(BUILD)/monocypher.o $(wildcard core/include/kk/*.h) sim/sim.h sim/check.h
	@mkdir -p $(BUILD)
	$(CXX) $(CXXFLAGS) $(INC) $(CORE_SRC) sim/vectors.cpp $(BUILD)/monocypher.o -o $@

$(BUILD)/kkudp: $(CORE_SRC) sim/udp_sim.cpp $(BUILD)/monocypher.o $(wildcard core/include/kk/*.h) sim/sim.h
	@mkdir -p $(BUILD)
	$(CXX) $(CXXFLAGS) $(INC) $(CORE_SRC) sim/udp_sim.cpp $(BUILD)/monocypher.o -o $@

.PHONY: test unit quick clean
test: $(BUILD)/kktest
	$(BUILD)/kktest all
unit: $(BUILD)/kktest
	$(BUILD)/kktest unit
quick: $(BUILD)/kktest
	$(BUILD)/kktest quick
clean:
	rm -rf $(BUILD)
