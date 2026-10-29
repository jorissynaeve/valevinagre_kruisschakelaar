#include <cstring>

#include "check.h"

void runUnit();
void runScenarios(bool quick);
void runSweep();
void runProbe();

int main(int argc, char** argv) {
  const char* what = argc > 1 ? argv[1] : "all";
  if (!strcmp(what, "unit") || !strcmp(what, "all")) runUnit();
  if (!strcmp(what, "scen") || !strcmp(what, "all")) runScenarios(false);
  if (!strcmp(what, "quick")) runScenarios(true);
  if (!strcmp(what, "sweep")) runSweep();
  if (!strcmp(what, "probe")) runProbe();
  std::printf("\n%d checks ok, %d failed\n", g_pass, g_fail);
  return g_fail ? 1 : 0;
}
