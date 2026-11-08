#include "vg/jsonflat.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

typedef struct {
    const char *s;
    size_t len, pos;
} cur_t;

static void ws(cur_t *c)
{
    while (c->pos < c->len && (c->s[c->pos] == ' ' || c->s[c->pos] == '\t' ||
                               c->s[c->pos] == '\r' || c->s[c->pos] == '\n')) {
        c->pos++;
    }
}

static bool lit(cur_t *c, const char *w)
{
    size_t n = strlen(w);
    if (c->pos + n <= c->len && memcmp(c->s + c->pos, w, n) == 0) {
        c->pos += n;
        return true;
    }
    return false;
}

static bool str(cur_t *c, char *out, size_t cap, const char **err)
{
    if (c->pos >= c->len || c->s[c->pos] != '"') {
        *err = "expected string";
        return false;
    }
    c->pos++;
    size_t n = 0;
    while (c->pos < c->len) {
        unsigned char ch = (unsigned char)c->s[c->pos++];
        if (ch == '"') {
            out[n] = '\0';
            return true;
        }
        if (ch == '\\') {
            *err = "escapes not supported";
            return false;
        }
        if (ch < 0x20) {
            *err = "control character in string";
            return false;
        }
        if (n + 1 >= cap) {
            *err = "string too long";
            return false;
        }
        out[n++] = (char)ch;
    }
    *err = "unterminated string";
    return false;
}

/* JSON number grammar: -?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)? */
static bool num(cur_t *c, vg_jf_kv_t *kv, const char **err)
{
    size_t st = c->pos;
    bool frac = false;
    if (c->pos < c->len && c->s[c->pos] == '-') c->pos++;
    if (c->pos >= c->len) goto bad;
    if (c->s[c->pos] == '0') {
        c->pos++;
    } else if (c->s[c->pos] >= '1' && c->s[c->pos] <= '9') {
        while (c->pos < c->len && c->s[c->pos] >= '0' && c->s[c->pos] <= '9') c->pos++;
    } else {
        goto bad;
    }
    if (c->pos < c->len && c->s[c->pos] == '.') {
        frac = true;
        c->pos++;
        size_t d = c->pos;
        while (c->pos < c->len && c->s[c->pos] >= '0' && c->s[c->pos] <= '9') c->pos++;
        if (c->pos == d) goto bad;
    }
    if (c->pos < c->len && (c->s[c->pos] == 'e' || c->s[c->pos] == 'E')) {
        frac = true;
        c->pos++;
        if (c->pos < c->len && (c->s[c->pos] == '+' || c->s[c->pos] == '-')) c->pos++;
        size_t d = c->pos;
        while (c->pos < c->len && c->s[c->pos] >= '0' && c->s[c->pos] <= '9') c->pos++;
        if (c->pos == d) goto bad;
    }
    char tmp[40];
    size_t n = c->pos - st;
    if (n >= sizeof tmp) {
        *err = "number too long";
        return false;
    }
    memcpy(tmp, c->s + st, n);
    tmp[n] = '\0';
    kv->d = strtod(tmp, NULL);
    if (!isfinite(kv->d)) {
        *err = "non-finite number";
        return false;
    }
    if (!frac && n <= 18) {
        kv->type = VG_JF_INT;
        kv->i = strtoll(tmp, NULL, 10);
    } else {
        kv->type = VG_JF_NUMBER;
    }
    return true;
bad:
    *err = "malformed number";
    return false;
}

bool vg_jf_parse(const char *s, size_t len, vg_jf_obj_t *out, const char **err)
{
    cur_t c = {s, len, 0};
    out->n = 0;
    ws(&c);
    if (!lit(&c, "{")) {
        *err = "expected object";
        return false;
    }
    ws(&c);
    if (lit(&c, "}")) goto end;
    for (;;) {
        if (out->n >= VG_JF_MAX_KEYS) {
            *err = "too many keys";
            return false;
        }
        vg_jf_kv_t *kv = &out->kv[out->n];
        memset(kv, 0, sizeof *kv);
        ws(&c);
        if (!str(&c, kv->key, sizeof kv->key, err)) return false;
        for (int k = 0; k < out->n; k++) {
            if (strcmp(out->kv[k].key, kv->key) == 0) {
                *err = "duplicate key";
                return false;
            }
        }
        ws(&c);
        if (!lit(&c, ":")) {
            *err = "expected ':'";
            return false;
        }
        ws(&c);
        if (c.pos >= c.len) {
            *err = "missing value";
            return false;
        }
        char ch = s[c.pos];
        if (ch == '"') {
            kv->type = VG_JF_STRING;
            if (!str(&c, kv->str, sizeof kv->str, err)) return false;
        } else if (ch == '{' || ch == '[') {
            *err = "nested values not allowed";
            return false;
        } else if (lit(&c, "true")) {
            kv->type = VG_JF_BOOL;
            kv->b = true;
        } else if (lit(&c, "false")) {
            kv->type = VG_JF_BOOL;
        } else if (lit(&c, "null")) {
            kv->type = VG_JF_NULL;
        } else if (!num(&c, kv, err)) {
            return false;
        }
        out->n++;
        ws(&c);
        if (lit(&c, ",")) continue;
        if (lit(&c, "}")) break;
        *err = "expected ',' or '}'";
        return false;
    }
end:
    ws(&c);
    if (c.pos != c.len) {
        *err = "trailing characters";
        return false;
    }
    return true;
}

const vg_jf_kv_t *vg_jf_get(const vg_jf_obj_t *o, const char *key)
{
    for (int k = 0; k < o->n; k++) {
        if (strcmp(o->kv[k].key, key) == 0) return &o->kv[k];
    }
    return NULL;
}
