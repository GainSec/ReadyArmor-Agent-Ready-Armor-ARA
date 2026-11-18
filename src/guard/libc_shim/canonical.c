// Path canonicalization for the shim.

#define _GNU_SOURCE

#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "shim.h"

static char *expand_home(const char *p) {
    if (!p || p[0] != '~') return strdup(p ? p : "");
    const char *home = getenv("HOME");
    if (!home || !*home) home = "/";
    if (p[1] == '\0') return strdup(home);
    if (p[1] == '/') {
        size_t len = strlen(home) + strlen(p + 1) + 1;
        char *out = malloc(len);
        snprintf(out, len, "%s%s", home, p + 1);
        return out;
    }
    // ~user/ — we don't try to resolve user lookup; leave as-is.
    return strdup(p);
}

static char *make_absolute(const char *p, const char *cwd_opt) {
    if (p[0] == '/') return strdup(p);
    char cwd_buf[PATH_MAX];
    const char *cwd = cwd_opt;
    if (!cwd) {
        if (!getcwd(cwd_buf, sizeof(cwd_buf))) return strdup(p);
        cwd = cwd_buf;
    }
    size_t len = strlen(cwd) + 1 + strlen(p) + 1;
    char *out = malloc(len);
    snprintf(out, len, "%s/%s", cwd, p);
    return out;
}

static char *normalize(const char *p) {
    // Simple normalizer: split on /, drop . segments, pop .. segments.
    // Returns a fresh malloc'd string, always absolute (leading /) if input was.
    size_t len = strlen(p);
    char *copy = strdup(p);
    if (!copy) return NULL;
    char *parts[PATH_MAX / 2];
    size_t n = 0;
    bool abs = (copy[0] == '/');
    char *save = NULL;
    for (char *tok = strtok_r(copy, "/", &save); tok; tok = strtok_r(NULL, "/", &save)) {
        if (strcmp(tok, ".") == 0 || tok[0] == '\0') continue;
        if (strcmp(tok, "..") == 0) {
            if (n > 0) n -= 1;
            continue;
        }
        parts[n++] = tok;
    }
    size_t total = abs ? 1 : 0;
    for (size_t i = 0; i < n; i++) total += strlen(parts[i]) + 1;
    if (total == 0) total = 1;
    char *out = malloc(total + 2);
    if (!out) {
        free(copy);
        return NULL;
    }
    char *p2 = out;
    if (abs) *p2++ = '/';
    for (size_t i = 0; i < n; i++) {
        size_t plen = strlen(parts[i]);
        memcpy(p2, parts[i], plen);
        p2 += plen;
        if (i + 1 < n) *p2++ = '/';
    }
    *p2 = '\0';
    if (out[0] == '\0') {
        // Empty result — return "/" if abs, else "."
        free(out);
        out = strdup(abs ? "/" : ".");
    }
    free(copy);
    return out;
}

// Resolve symlinks where possible. If the target doesn't exist, resolve the
// longest existing prefix and append the unresolved tail.
static char *resolve_symlinks(const char *p) {
    char *canon = realpath(p, NULL);
    if (canon) return canon;

    // Walk up until we find an existing component.
    char *copy = strdup(p);
    if (!copy) return strdup(p);
    char tail[PATH_MAX] = "";
    for (;;) {
        char *slash = strrchr(copy, '/');
        if (!slash || slash == copy) {
            // Give up; return best-effort normalized form.
            char *out = normalize(p);
            free(copy);
            return out ? out : strdup(p);
        }
        // Append the trailing component (in reverse: we're prepending).
        char before[PATH_MAX];
        snprintf(before, sizeof(before), "%s/%s", slash + 1, tail);
        strncpy(tail, before, sizeof(tail));
        tail[sizeof(tail) - 1] = '\0';
        *slash = '\0';
        char *maybe = realpath(copy, NULL);
        if (maybe) {
            size_t len = strlen(maybe) + 1 + strlen(tail) + 1;
            char *out = malloc(len);
            snprintf(out, len, "%s/%s", maybe, tail);
            free(maybe);
            free(copy);
            char *normalized = normalize(out);
            free(out);
            return normalized ? normalized : strdup(p);
        }
    }
}

char *ara_canonicalize(const char *path, const char *cwd) {
    if (!path || !*path) return NULL;
    char *with_home = expand_home(path);
    if (!with_home) return NULL;
    char *abs = make_absolute(with_home, cwd);
    free(with_home);
    if (!abs) return NULL;
    char *resolved = resolve_symlinks(abs);
    free(abs);
    return resolved;
}
