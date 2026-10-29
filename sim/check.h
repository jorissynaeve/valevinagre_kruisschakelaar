#pragma once
#include <cstdio>

inline int g_pass = 0, g_fail = 0;

#define CHECK(c)                                                  \
  do {                                                            \
    if (c) {                                                      \
      g_pass++;                                                   \
    } else {                                                      \
      g_fail++;                                                   \
      std::printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #c);  \
    }                                                             \
  } while (0)

#define CHECKM(c, ...)                                            \
  do {                                                            \
    if (c) {                                                      \
      g_pass++;                                                   \
    } else {                                                      \
      g_fail++;                                                   \
      std::printf("  FAIL %s:%d  %s -- ", __FILE__, __LINE__, #c); \
      std::printf(__VA_ARGS__);                                   \
      std::printf("\n");                                          \
    }                                                             \
  } while (0)
