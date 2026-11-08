/* Strict parser for ONE flat JSON object (no nesting) used by the station
 * command protocol. Bounded: fixed key/value sizes, no allocation.
 * Rejects: nested objects/arrays, duplicate keys, backslash escapes,
 * control characters, non-finite or malformed numbers, trailing garbage. */
#ifndef VG_JSONFLAT_H
#define VG_JSONFLAT_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define VG_JF_MAX_KEYS 12
#define VG_JF_KEY_LEN 24     /* incl. NUL */
#define VG_JF_STR_LEN 72     /* incl. NUL */

typedef enum { VG_JF_STRING, VG_JF_INT, VG_JF_NUMBER, VG_JF_BOOL, VG_JF_NULL } vg_jf_type_t;

typedef struct {
    char key[VG_JF_KEY_LEN];
    vg_jf_type_t type;
    char str[VG_JF_STR_LEN];
    int64_t i;
    double d;
    bool b;
} vg_jf_kv_t;

typedef struct {
    vg_jf_kv_t kv[VG_JF_MAX_KEYS];
    int n;
} vg_jf_obj_t;

/* Returns true on success; on failure *err names the problem. */
bool vg_jf_parse(const char *s, size_t len, vg_jf_obj_t *out, const char **err);
const vg_jf_kv_t *vg_jf_get(const vg_jf_obj_t *o, const char *key);

#ifdef __cplusplus
}
#endif
#endif
