/* Minimal dependency-free test harness for host tests. */
#ifndef VG_TEST_H
#define VG_TEST_H

#include <stdio.h>
#include <string.h>

static int vg_failures;
static int vg_checks;

#define CHECK(cond)                                                              \
    do {                                                                         \
        vg_checks++;                                                             \
        if (!(cond)) {                                                           \
            vg_failures++;                                                       \
            fprintf(stderr, "%s:%d: CHECK failed: %s\n", __FILE__, __LINE__, #cond); \
        }                                                                        \
    } while (0)

#define RUN(fn)                                                                  \
    do {                                                                         \
        int before = vg_failures;                                                \
        fn();                                                                    \
        printf("%s %s\n", vg_failures == before ? "ok  " : "FAIL", #fn);         \
    } while (0)

#define DONE()                                                                   \
    (printf("%d checks, %d failures\n", vg_checks, vg_failures), vg_failures ? 1 : 0)

#endif
