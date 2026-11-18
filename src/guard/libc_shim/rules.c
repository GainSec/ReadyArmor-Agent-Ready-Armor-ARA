// Rule cache + matcher.
//
// At hello time the supervisor sends sync_rules. We store them in memory,
// compile the glob patterns to POSIX regex (same semantics as the Python /
// JS matchers: `*` = non-slash, `**` = any depth, `?` = single non-slash).

#define _GNU_SOURCE

#include <ctype.h>
#include <regex.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "shim.h"

// Convert a glob pattern to a POSIX extended regex. Caller frees.
static char *glob_to_regex(const char *pattern) {
    size_t cap = strlen(pattern) * 4 + 4;
    char *out = malloc(cap);
    if (!out) return NULL;
    size_t j = 0;
    out[j++] = '^';
    size_t i = 0;
    while (pattern[i]) {
        char c = pattern[i];
        if (c == '*') {
            if (pattern[i + 1] == '*') {
                out[j++] = '.';
                out[j++] = '*';
                i += 2;
                if (pattern[i] == '/') i += 1;
                continue;
            }
            out[j++] = '[';
            out[j++] = '^';
            out[j++] = '/';
            out[j++] = ']';
            out[j++] = '*';
            i += 1;
            continue;
        }
        if (c == '?') {
            out[j++] = '[';
            out[j++] = '^';
            out[j++] = '/';
            out[j++] = ']';
            i += 1;
            continue;
        }
        if (strchr(".+^$(){}|[]\\", c)) {
            out[j++] = '\\';
        }
        out[j++] = c;
        i += 1;
    }
    out[j++] = '$';
    out[j] = '\0';
    return out;
}

int ara_rules_add_path(ara_rules_t *rules, const char *rule_id, const char *pattern) {
    ara_rule_t *grown = realloc(rules->path_rules,
                                (rules->path_count + 1) * sizeof(ara_rule_t));
    if (!grown) return -1;
    rules->path_rules = grown;
    ara_rule_t *slot = &rules->path_rules[rules->path_count];
    slot->rule_id = strdup(rule_id);
    slot->pattern = strdup(pattern);
    slot->regex = glob_to_regex(pattern);
    slot->compiled = NULL;
    if (slot->regex) {
        regex_t *r = malloc(sizeof(regex_t));
        if (r) {
            if (regcomp(r, slot->regex, REG_EXTENDED | REG_NOSUB) == 0) {
                slot->compiled = r;
            } else {
                free(r);
            }
        }
    }
    rules->path_count += 1;
    return 0;
}

int ara_rules_add_command(ara_rules_t *rules, const char *rule_id, const char *pattern) {
    ara_rule_t *grown = realloc(rules->command_rules,
                                (rules->command_count + 1) * sizeof(ara_rule_t));
    if (!grown) return -1;
    rules->command_rules = grown;
    ara_rule_t *slot = &rules->command_rules[rules->command_count];
    slot->rule_id = strdup(rule_id);
    slot->pattern = strdup(pattern);
    slot->regex = glob_to_regex(pattern);
    slot->compiled = NULL;
    if (slot->regex) {
        regex_t *r = malloc(sizeof(regex_t));
        if (r) {
            if (regcomp(r, slot->regex, REG_EXTENDED | REG_NOSUB) == 0) {
                slot->compiled = r;
            } else {
                free(r);
            }
        }
    }
    rules->command_count += 1;
    return 0;
}

const char *ara_match_path(ara_ctx_t *ctx, const char *canonical) {
    if (!ctx || !canonical) return NULL;
    for (size_t i = 0; i < ctx->rules.path_count; i++) {
        regex_t *r = ctx->rules.path_rules[i].compiled;
        if (!r) continue;
        if (regexec(r, canonical, 0, NULL, 0) == 0) {
            return ctx->rules.path_rules[i].rule_id;
        }
    }
    return NULL;
}

static const char *base_name(const char *s) {
    if (!s) return "";
    const char *slash = strrchr(s, '/');
    return slash ? slash + 1 : s;
}

static const char *match_command_candidate(ara_ctx_t *ctx, const char *candidate) {
    if (!ctx || !candidate || !*candidate) return NULL;
    for (size_t i = 0; i < ctx->rules.command_count; i++) {
        regex_t *r = ctx->rules.command_rules[i].compiled;
        if (!r) continue;
        if (regexec(r, candidate, 0, NULL, 0) == 0) {
            return ctx->rules.command_rules[i].rule_id;
        }
    }
    return NULL;
}

static int is_shell_name(const char *base) {
    if (!base) return 0;
    return strcmp(base, "sh") == 0 ||
           strcmp(base, "bash") == 0 ||
           strcmp(base, "dash") == 0 ||
           strcmp(base, "zsh") == 0 ||
           strcmp(base, "ksh") == 0 ||
           strcmp(base, "ash") == 0;
}

static void first_token(const char *s, char *out, size_t out_len) {
    if (!out || out_len == 0) return;
    out[0] = '\0';
    if (!s) return;
    while (*s && isspace((unsigned char)*s)) s++;
    size_t n = 0;
    while (*s && !isspace((unsigned char)*s) && n + 1 < out_len) {
        out[n++] = *s++;
    }
    out[n] = '\0';
}

const char *ara_match_command(ara_ctx_t *ctx, const char *command, char *const argv[]) {
    if (!ctx || ctx->rules.command_count == 0) return NULL;
    const char *hit = match_command_candidate(ctx, command);
    if (hit) return hit;

    const char *base = base_name(command);
    hit = match_command_candidate(ctx, base);
    if (hit) return hit;

    if (argv && argv[0]) {
        hit = match_command_candidate(ctx, argv[0]);
        if (hit) return hit;
        hit = match_command_candidate(ctx, base_name(argv[0]));
        if (hit) return hit;
    }

    char joined[4096];
    joined[0] = '\0';
    if (command && *command) {
        strncat(joined, command, sizeof(joined) - strlen(joined) - 1);
        if (argv) {
            for (size_t i = 1; argv[i]; i++) {
                strncat(joined, " ", sizeof(joined) - strlen(joined) - 1);
                strncat(joined, argv[i], sizeof(joined) - strlen(joined) - 1);
            }
        }
        hit = match_command_candidate(ctx, joined);
        if (hit) return hit;
    }

    if (is_shell_name(base) && argv) {
        for (size_t i = 1; argv[i]; i++) {
            if (strcmp(argv[i], "-c") != 0 && strcmp(argv[i], "-lc") != 0) {
                continue;
            }
            if (!argv[i + 1]) break;
            const char *inner = argv[i + 1];
            hit = match_command_candidate(ctx, inner);
            if (hit) return hit;
            char tok[512];
            first_token(inner, tok, sizeof(tok));
            hit = match_command_candidate(ctx, tok);
            if (hit) return hit;
            hit = match_command_candidate(ctx, base_name(tok));
            if (hit) return hit;
            break;
        }
    }

    return NULL;
}

static void add_net_rule(
    ara_net_rule_t **slot_array, size_t *count,
    const char *rule_id, const char *pattern,
    const char *host, const char *port
) {
    ara_net_rule_t *grown = realloc(*slot_array,
                                    ((*count) + 1) * sizeof(ara_net_rule_t));
    if (!grown) return;
    *slot_array = grown;
    ara_net_rule_t *slot = &(*slot_array)[*count];
    slot->rule_id = strdup(rule_id);
    slot->pattern = strdup(pattern);
    // Lowercase host for case-insensitive match.
    char *hlow = strdup(host ? host : "*");
    if (hlow) {
        for (char *p = hlow; *p; p++) *p = (char)tolower((unsigned char)*p);
    }
    slot->host = hlow;
    slot->port = strdup(port ? port : "*");
    slot->host_compiled = NULL;
    slot->port_compiled = NULL;
    if (slot->host) {
        char *hr = glob_to_regex(slot->host);
        if (hr) {
            regex_t *r = malloc(sizeof(regex_t));
            if (r && regcomp(r, hr, REG_EXTENDED | REG_NOSUB) == 0) {
                slot->host_compiled = r;
            } else {
                free(r);
            }
            free(hr);
        }
    }
    if (slot->port) {
        char *pr = glob_to_regex(slot->port);
        if (pr) {
            regex_t *r = malloc(sizeof(regex_t));
            if (r && regcomp(r, pr, REG_EXTENDED | REG_NOSUB) == 0) {
                slot->port_compiled = r;
            } else {
                free(r);
            }
            free(pr);
        }
    }
    (*count) += 1;
}

void ara_rules_add_net(ara_rules_t *rules, const char *rule_id,
                       const char *pattern,
                       const char *host, const char *port) {
    add_net_rule(&rules->net_rules, &rules->net_count,
                 rule_id, pattern, host, port);
}

void ara_rules_add_allow_net(ara_rules_t *rules, const char *rule_id,
                             const char *pattern,
                             const char *host, const char *port) {
    add_net_rule(&rules->allow_net_rules, &rules->allow_net_count,
                 rule_id, pattern, host, port);
}

// ---- nav + perm (path-glob rulesets shared with bulwark) ----------

static int add_path_glob_rule(
    ara_rule_t **slot_array, size_t *count,
    const char *rule_id, const char *pattern
) {
    ara_rule_t *grown = realloc(*slot_array,
                                ((*count) + 1) * sizeof(ara_rule_t));
    if (!grown) return -1;
    *slot_array = grown;
    ara_rule_t *slot = &(*slot_array)[*count];
    slot->rule_id = strdup(rule_id);
    slot->pattern = strdup(pattern);
    slot->regex = glob_to_regex(pattern);
    slot->compiled = NULL;
    if (slot->regex) {
        regex_t *r = malloc(sizeof(regex_t));
        if (r) {
            if (regcomp(r, slot->regex, REG_EXTENDED | REG_NOSUB) == 0) {
                slot->compiled = r;
            } else {
                free(r);
            }
        }
    }
    (*count) += 1;
    return 0;
}

int ara_rules_add_nav(ara_rules_t *rules, const char *rule_id, const char *pattern) {
    return add_path_glob_rule(&rules->nav_rules, &rules->nav_count, rule_id, pattern);
}

int ara_rules_add_perm(ara_rules_t *rules, const char *rule_id, const char *pattern) {
    return add_path_glob_rule(&rules->perm_rules, &rules->perm_count, rule_id, pattern);
}

static const char *match_path_glob_set(
    const ara_rule_t *set, size_t count, const char *canonical
) {
    if (!canonical) return NULL;
    for (size_t i = 0; i < count; i++) {
        regex_t *r = set[i].compiled;
        if (!r) continue;
        if (regexec(r, canonical, 0, NULL, 0) == 0) return set[i].rule_id;
    }
    return NULL;
}

const char *ara_match_nav(ara_ctx_t *ctx, const char *canonical) {
    if (!ctx) return NULL;
    return match_path_glob_set(ctx->rules.nav_rules, ctx->rules.nav_count, canonical);
}

const char *ara_match_perm(ara_ctx_t *ctx, const char *canonical) {
    if (!ctx) return NULL;
    return match_path_glob_set(ctx->rules.perm_rules, ctx->rules.perm_count, canonical);
}

const char *ara_match_net(ara_ctx_t *ctx, const char *host, int port) {
    if (!ctx || !host) return NULL;
    // Normalize host to lower for matching.
    char hbuf[256];
    size_t hn = 0;
    for (const char *p = host; *p && hn + 1 < sizeof(hbuf); p++) {
        hbuf[hn++] = (char)tolower((unsigned char)*p);
    }
    hbuf[hn] = '\0';
    char pbuf[16];
    snprintf(pbuf, sizeof(pbuf), "%d", port);
    for (size_t i = 0; i < ctx->rules.net_count; i++) {
        regex_t *hr = ctx->rules.net_rules[i].host_compiled;
        regex_t *pr = ctx->rules.net_rules[i].port_compiled;
        if (!hr) continue;
        if (regexec(hr, hbuf, 0, NULL, 0) != 0) continue;
        // port "*" → match all; otherwise regex must match.
        int port_ok = 1;
        const char *ppat = ctx->rules.net_rules[i].port;
        if (ppat && strcmp(ppat, "*") != 0) {
            port_ok = (pr && regexec(pr, pbuf, 0, NULL, 0) == 0);
        }
        if (port_ok) return ctx->rules.net_rules[i].rule_id;
    }
    return NULL;
}

const char *ara_match_allow_net(ara_ctx_t *ctx, const char *host, int port) {
    if (!ctx || !host) return NULL;
    char hbuf[256];
    size_t hn = 0;
    for (const char *p = host; *p && hn + 1 < sizeof(hbuf); p++) {
        hbuf[hn++] = (char)tolower((unsigned char)*p);
    }
    hbuf[hn] = '\0';
    char pbuf[16];
    snprintf(pbuf, sizeof(pbuf), "%d", port);
    for (size_t i = 0; i < ctx->rules.allow_net_count; i++) {
        regex_t *hr = ctx->rules.allow_net_rules[i].host_compiled;
        regex_t *pr = ctx->rules.allow_net_rules[i].port_compiled;
        if (!hr) continue;
        if (regexec(hr, hbuf, 0, NULL, 0) != 0) continue;
        int port_ok = 1;
        const char *ppat = ctx->rules.allow_net_rules[i].port;
        if (ppat && strcmp(ppat, "*") != 0) {
            port_ok = (pr && regexec(pr, pbuf, 0, NULL, 0) == 0);
        }
        if (port_ok) return ctx->rules.allow_net_rules[i].rule_id;
    }
    return NULL;
}
