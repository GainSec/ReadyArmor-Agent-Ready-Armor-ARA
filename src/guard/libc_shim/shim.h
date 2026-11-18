#ifndef ARA_SHIM_H
#define ARA_SHIM_H

#include <stdbool.h>
#include <stddef.h>

typedef struct ara_rule {
    char *rule_id;
    char *pattern; // original glob, for audit
    char *regex;   // compiled form's source (kept for re-use)
    void *compiled; // regex_t *
} ara_rule_t;

typedef struct ara_net_rule {
    char *rule_id;
    char *pattern;        // original "<host>:<port>" form
    char *host;           // host glob (lowercased at parse)
    char *port;           // port glob (as string)
    void *host_compiled;  // regex_t *
    void *port_compiled;  // regex_t *
} ara_net_rule_t;

typedef struct ara_rules {
    ara_rule_t *path_rules;
    size_t path_count;
    ara_rule_t *command_rules;
    size_t command_count;
    ara_net_rule_t *net_rules;
    size_t net_count;
    ara_net_rule_t *allow_net_rules;
    size_t allow_net_count;
    // Bulwark nav (directory ops) and perm (permission/attr change)
    // rules. Both match on a path glob, same matcher shape as path_rules.
    ara_rule_t *nav_rules;
    size_t nav_count;
    ara_rule_t *perm_rules;
    size_t perm_count;
} ara_rules_t;

typedef struct ara_ctx {
    int sock_fd;
    char *session;
    char *token;
    char *shim_path;
    ara_rules_t rules;
    bool initialized;
    bool shadow_mode;  // dry-run: matches audit but allow
} ara_ctx_t;

// Idempotent; safe to call from any hook.
ara_ctx_t *ara_ctx(void);

// Returns rule_id if path matches any deny_path rule after canonicalization;
// NULL otherwise. ``raw`` is the as-given path; ``canonical`` is the realpath'd
// form. Both may be equal.
const char *ara_match_path(ara_ctx_t *ctx, const char *canonical);

// Returns rule_id if command/argv matches any deny_command rule; NULL otherwise.
const char *ara_match_command(ara_ctx_t *ctx, const char *command, char *const argv[]);

// Returns rule_id if (host,port) matches any deny_net rule; NULL otherwise.
// host may be an IP literal or a hostname.
const char *ara_match_net(ara_ctx_t *ctx, const char *host, int port);

// Returns rule_id if (host,port) matches any allow_net rule; NULL otherwise.
const char *ara_match_allow_net(ara_ctx_t *ctx, const char *host, int port);

// Bulwark nav / perm matchers. Both take the canonicalized path and
// return the first matching rule_id (or NULL). Thin wrappers over the
// same regex machinery ``ara_match_path`` uses.
const char *ara_match_nav(ara_ctx_t *ctx, const char *canonical);
const char *ara_match_perm(ara_ctx_t *ctx, const char *canonical);

// Add a nav / perm rule (used by sync_rules parsing).
int ara_rules_add_command(ara_rules_t *rules, const char *rule_id, const char *pattern);
int ara_rules_add_nav(ara_rules_t *rules, const char *rule_id, const char *pattern);
int ara_rules_add_perm(ara_rules_t *rules, const char *rule_id, const char *pattern);

// Add a net rule to a rules struct (used by sync_rules parsing).
void ara_rules_add_net(ara_rules_t *rules, const char *rule_id,
                       const char *pattern, const char *host, const char *port);
void ara_rules_add_allow_net(ara_rules_t *rules, const char *rule_id,
                             const char *pattern, const char *host, const char *port);

// Fire-and-forget audit event. NULL fields are omitted.
void ara_emit_policy_hit(ara_ctx_t *ctx,
                         const char *op,
                         const char *path,
                         const char *raw_path,
                         const char *verdict,
                         const char *reason,
                         const char *rule_id);

void ara_emit_spawn_event(ara_ctx_t *ctx,
                          const char *command,
                          char *const argv[]);

// Canonicalize a path (realpath + fallback). Caller frees.
char *ara_canonicalize(const char *path, const char *cwd);

#endif
