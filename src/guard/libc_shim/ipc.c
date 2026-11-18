// IPC client: connect to ARA supervisor, hello handshake, receive sync_rules.
//
// Minimal JSON: we hand-serialize outgoing messages (known shape) and do a
// tiny inline parser for the hello_ack reply (we only care about a few
// fields). Keeps the shim dependency-free.

#define _GNU_SOURCE

#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#include "shim.h"

int ara_rules_add_path(ara_rules_t *rules, const char *rule_id, const char *pattern);
int ara_rules_add_command(ara_rules_t *rules, const char *rule_id, const char *pattern);

static void start_heartbeat_thread(void);

extern __thread int ara_in_hook;

static pthread_mutex_t init_mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t io_mu = PTHREAD_MUTEX_INITIALIZER;
static ara_ctx_t g_ctx;
static bool connecting = false;

static ssize_t read_line(int fd, char *buf, size_t cap) {
    size_t n = 0;
    while (n + 1 < cap) {
        char c;
        ssize_t r = read(fd, &c, 1);
        if (r <= 0) return r == 0 ? (ssize_t)n : -1;
        buf[n++] = c;
        if (c == '\n') break;
    }
    buf[n] = '\0';
    return (ssize_t)n;
}

static int write_all(int fd, const char *buf, size_t len) {
    size_t n = 0;
    while (n < len) {
        ssize_t w = write(fd, buf + n, len - n);
        if (w < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        n += (size_t)w;
    }
    return 0;
}

// Tiny JSON string escaper. Returns malloc'd string.
static char *json_escape(const char *s) {
    if (!s) return strdup("");
    size_t len = 0;
    for (const char *p = s; *p; p++) {
        if (*p == '"' || *p == '\\' || *p == '\n' || *p == '\r' || *p == '\t' || (unsigned char)*p < 0x20) {
            len += 2;
        } else {
            len += 1;
        }
    }
    char *out = malloc(len + 1);
    if (!out) return NULL;
    char *o = out;
    for (const char *p = s; *p; p++) {
        unsigned char c = (unsigned char)*p;
        if (c == '"') { *o++ = '\\'; *o++ = '"'; }
        else if (c == '\\') { *o++ = '\\'; *o++ = '\\'; }
        else if (c == '\n') { *o++ = '\\'; *o++ = 'n'; }
        else if (c == '\r') { *o++ = '\\'; *o++ = 'r'; }
        else if (c == '\t') { *o++ = '\\'; *o++ = 't'; }
        else if (c < 0x20) { o += sprintf(o, "\\u%04x", c); }
        else *o++ = (char)c;
    }
    *o = '\0';
    return out;
}

// Very small extractor: find "key":"value" or "key":value (number).
// For arrays / nested objects we bail. Sufficient for our reply shape.
static char *json_find_string(const char *blob, const char *key) {
    char needle[128];
    snprintf(needle, sizeof(needle), "\"%s\"", key);
    const char *p = strstr(blob, needle);
    if (!p) return NULL;
    p += strlen(needle);
    while (*p && (isspace((unsigned char)*p) || *p == ':')) p++;
    if (*p != '"') return NULL;
    p += 1;
    const char *start = p;
    while (*p && *p != '"') {
        if (*p == '\\' && *(p + 1)) p += 2;
        else p += 1;
    }
    size_t n = (size_t)(p - start);
    char *out = malloc(n + 1);
    memcpy(out, start, n);
    out[n] = '\0';
    return out;
}

static void parse_sync_rules(const char *blob) {
    // Extract "sync_rules":{ ... }.
    const char *sr = strstr(blob, "\"sync_rules\"");
    if (!sr) return;
    // shadow_mode flag — look for "shadow_mode":true within sync_rules scope.
    const char *sm = strstr(sr, "\"shadow_mode\"");
    if (sm) {
        const char *p = sm + strlen("\"shadow_mode\"");
        while (*p && (*p == ':' || *p == ' ')) p++;
        if (p[0] == 't' && p[1] == 'r' && p[2] == 'u' && p[3] == 'e') {
            g_ctx.shadow_mode = true;
        } else {
            g_ctx.shadow_mode = false;
        }
    }
    const char *dp = strstr(sr, "\"deny_path\"");
    if (!dp) return;
    const char *arr = strchr(dp, '[');
    if (!arr) return;
    const char *end = strchr(arr, ']');
    if (!end) return;

    const char *p = arr + 1;
    while (p < end) {
        const char *obj_start = strchr(p, '{');
        if (!obj_start || obj_start >= end) break;
        const char *obj_end = strchr(obj_start, '}');
        if (!obj_end || obj_end > end) break;
        size_t olen = (size_t)(obj_end - obj_start + 1);
        char *obj = malloc(olen + 1);
        memcpy(obj, obj_start, olen);
        obj[olen] = '\0';
        char *rule_id = json_find_string(obj, "rule_id");
        char *pattern = json_find_string(obj, "pattern");
        if (rule_id && pattern) {
            ara_rules_add_path(&g_ctx.rules, rule_id, pattern);
        }
        free(rule_id);
        free(pattern);
        free(obj);
        p = obj_end + 1;
    }

    const char *dc = strstr(sr, "\"deny_command\"");
    if (dc) {
        const char *carr = strchr(dc, '[');
        const char *cend = carr ? strchr(carr, ']') : NULL;
        if (carr && cend) {
            const char *q = carr + 1;
            while (q < cend) {
                const char *os = strchr(q, '{');
                if (!os || os >= cend) break;
                const char *oe = strchr(os, '}');
                if (!oe || oe > cend) break;
                size_t olen = (size_t)(oe - os + 1);
                char *obj = malloc(olen + 1);
                memcpy(obj, os, olen);
                obj[olen] = '\0';
                char *rid = json_find_string(obj, "rule_id");
                char *pat = json_find_string(obj, "pattern");
                if (rid && pat) {
                    ara_rules_add_command(&g_ctx.rules, rid, pat);
                }
                free(rid);
                free(pat);
                free(obj);
                q = oe + 1;
            }
        }
    }

    // deny_nav + deny_perm: same object shape as deny_path (rule_id + pattern).
    // Bulwark's nav + perm layers. Consulted by new mkdir/rmdir/chdir/chmod/
    // chown hooks, but safe to parse unconditionally even on builds without
    // those hooks (the rule cache just goes unused).
    for (int which = 0; which < 2; which++) {
        const char *tag = (which == 0) ? "\"deny_nav\"" : "\"deny_perm\"";
        const char *sect = strstr(sr, tag);
        if (!sect) continue;
        const char *sarr = strchr(sect, '[');
        const char *send = sarr ? strchr(sarr, ']') : NULL;
        if (!sarr || !send) continue;
        const char *q = sarr + 1;
        while (q < send) {
            const char *os = strchr(q, '{');
            if (!os || os >= send) break;
            const char *oe = strchr(os, '}');
            if (!oe || oe > send) break;
            size_t olen = (size_t)(oe - os + 1);
            char *obj = malloc(olen + 1);
            memcpy(obj, os, olen);
            obj[olen] = '\0';
            char *rid = json_find_string(obj, "rule_id");
            char *pat = json_find_string(obj, "pattern");
            if (rid && pat) {
                if (which == 0) ara_rules_add_nav(&g_ctx.rules, rid, pat);
                else            ara_rules_add_perm(&g_ctx.rules, rid, pat);
            }
            free(rid); free(pat);
            free(obj);
            q = oe + 1;
        }
    }

    // deny_net: objects have rule_id, pattern, host, port.
    const char *dn = strstr(sr, "\"deny_net\"");
    if (dn) {
        const char *narr = strchr(dn, '[');
        const char *nend = narr ? strchr(narr, ']') : NULL;
        if (narr && nend) {
            const char *q = narr + 1;
            while (q < nend) {
                const char *os = strchr(q, '{');
                if (!os || os >= nend) break;
                const char *oe = strchr(os, '}');
                if (!oe || oe > nend) break;
                size_t olen = (size_t)(oe - os + 1);
                char *obj = malloc(olen + 1);
                memcpy(obj, os, olen);
                obj[olen] = '\0';
                char *rid  = json_find_string(obj, "rule_id");
                char *pat  = json_find_string(obj, "pattern");
                char *host = json_find_string(obj, "host");
                char *port = json_find_string(obj, "port");
                if (rid && pat) {
                    ara_rules_add_net(&g_ctx.rules, rid, pat,
                                      host ? host : "*",
                                      port ? port : "*");
                }
                free(rid); free(pat); free(host); free(port);
                free(obj);
                q = oe + 1;
            }
        }
    }

    // allow_net: objects have rule_id, pattern, host, port. These are
    // checked before deny_net in connect() so default-deny egress can keep
    // explicit LLM/control-plane routes open.
    const char *an = strstr(sr, "\"allow_net\"");
    if (an) {
        const char *narr = strchr(an, '[');
        const char *nend = narr ? strchr(narr, ']') : NULL;
        if (narr && nend) {
            const char *q = narr + 1;
            while (q < nend) {
                const char *os = strchr(q, '{');
                if (!os || os >= nend) break;
                const char *oe = strchr(os, '}');
                if (!oe || oe > nend) break;
                size_t olen = (size_t)(oe - os + 1);
                char *obj = malloc(olen + 1);
                memcpy(obj, os, olen);
                obj[olen] = '\0';
                char *rid  = json_find_string(obj, "rule_id");
                char *pat  = json_find_string(obj, "pattern");
                char *host = json_find_string(obj, "host");
                char *port = json_find_string(obj, "port");
                if (rid && pat) {
                    ara_rules_add_allow_net(&g_ctx.rules, rid, pat,
                                            host ? host : "*",
                                            port ? port : "*");
                }
                free(rid); free(pat); free(host); free(port);
                free(obj);
                q = oe + 1;
            }
        }
    }
}

static int do_connect_and_handshake(void) {
    const char *sock_path = getenv("ARA_SOCKET");
    const char *token = getenv("ARA_SESSION_TOKEN");
    const char *session = getenv("ARA_SESSION_ID");
    if (!sock_path || !token) return -1;

    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) return -1;
    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, sock_path, sizeof(addr.sun_path) - 1);
    int connected = 0;
    int last_errno = 0;
    for (int attempt = 0; attempt < 50; attempt++) {
        if (connect(fd, (struct sockaddr *)&addr, sizeof(addr)) == 0) {
            connected = 1;
            break;
        }
        last_errno = errno;
        if (last_errno != ENOENT && last_errno != ECONNREFUSED && last_errno != EAGAIN) {
            break;
        }
        struct timespec ts = {0, 10 * 1000 * 1000};
        nanosleep(&ts, NULL);
    }
    if (!connected) {
        if (getenv("ARA_SHIM_DEBUG")) {
            fprintf(stderr, "[ara-shim] handshake connect failed path=%s errno=%d\n",
                    sock_path, last_errno);
        }
        close(fd);
        return -1;
    }

    char *esc_token = json_escape(token);
    char *esc_session = json_escape(session ? session : "");
    char hello[2048];
    int n = snprintf(hello, sizeof(hello),
                     "{\"type\":\"hello\",\"token\":\"%s\",\"session\":\"%s\","
                     "\"pid\":%d,\"ppid\":%d,\"runtime\":\"libc-shim\",\"seq\":1}\n",
                     esc_token, esc_session, (int)getpid(), (int)getppid());
    free(esc_token);
    free(esc_session);
    if (n < 0 || write_all(fd, hello, (size_t)n) < 0) {
        if (getenv("ARA_SHIM_DEBUG")) {
            fprintf(stderr, "[ara-shim] handshake write failed errno=%d\n", errno);
        }
        close(fd);
        return -1;
    }

    char reply[16384];
    ssize_t r = read_line(fd, reply, sizeof(reply));
    if (r <= 0) {
        if (getenv("ARA_SHIM_DEBUG")) {
            fprintf(stderr, "[ara-shim] handshake read failed r=%zd errno=%d\n", r, errno);
        }
        close(fd);
        return -1;
    }

    char *verdict = json_find_string(reply, "verdict");
    if (!verdict || strcmp(verdict, "accept") != 0) {
        if (getenv("ARA_SHIM_DEBUG")) {
            fprintf(stderr, "[ara-shim] handshake rejected reply_prefix=%.*s\n",
                    200, reply);
        }
        free(verdict);
        close(fd);
        return -1;
    }
    free(verdict);

    parse_sync_rules(reply);

    g_ctx.sock_fd = fd;
    g_ctx.session = session ? strdup(session) : NULL;
    g_ctx.token = strdup(token);

    // Resolve our own .so path so exec hooks can reinstate LD_PRELOAD.
    char link[64];
    snprintf(link, sizeof(link), "/proc/self/maps");
    FILE *maps = fopen(link, "r");
    if (maps) {
        char line[4096];
        while (fgets(line, sizeof(line), maps)) {
            if (strstr(line, "libara_shim")) {
                char *slash = strchr(line, '/');
                if (slash) {
                    char *nl = strchr(slash, '\n');
                    if (nl) *nl = '\0';
                    g_ctx.shim_path = strdup(slash);
                    break;
                }
            }
        }
        fclose(maps);
    }

    g_ctx.initialized = true;
    start_heartbeat_thread();
    return 0;
}

ara_ctx_t *ara_ctx(void) {
    if (g_ctx.initialized) return &g_ctx;
    // Block recursion during init: while we're setting up (connecting to
    // the supervisor, fopen'ing /proc, etc.), any hook fired from inside
    // our own code must bypass enforcement. ara_in_hook is the thread-local
    // re-entry flag the hook layer reads.
    ara_in_hook++;
    pthread_mutex_lock(&init_mu);
    if (!g_ctx.initialized && !connecting) {
        connecting = true;
        do_connect_and_handshake();
        connecting = false;
    }
    pthread_mutex_unlock(&init_mu);
    ara_in_hook--;
    return &g_ctx;
}

static void send_line(ara_ctx_t *ctx, const char *line, size_t n) {
    if (!ctx || ctx->sock_fd <= 0) return;
    pthread_mutex_lock(&io_mu);
    (void)write_all(ctx->sock_fd, line, n);
    pthread_mutex_unlock(&io_mu);
}

// Heartbeat thread. The supervisor's watchdog SIGTERMs any guard pid
// that hasn't pinged in ARA_HEARTBEAT_TIMEOUT_S (default 10s). We can't
// rely on hooked syscalls firing for liveness — a process blocked on a
// long network read (e.g. an LLM inference call) makes no syscalls we
// hook, so without an active heartbeat the supervisor mistakenly kills
// it. Period configurable via ARA_HEARTBEAT_INTERVAL_S (default 3s).
static pthread_t heartbeat_tid;
static bool heartbeat_started = false;

static void *heartbeat_loop(void *arg) {
    (void)arg;
    long interval_s = 3;
    const char *env = getenv("ARA_HEARTBEAT_INTERVAL_S");
    if (env && *env) {
        char *endp;
        long v = strtol(env, &endp, 10);
        if (endp != env && v >= 1 && v <= 60) interval_s = v;
    }
    char line[128];
    int n = snprintf(line, sizeof(line),
                     "{\"type\":\"event\",\"kind\":\"heartbeat\",\"pid\":%d}\n",
                     (int)getpid());
    if (n <= 0) return NULL;
    while (1) {
        struct timespec ts = { .tv_sec = interval_s, .tv_nsec = 0 };
        nanosleep(&ts, NULL);
        if (!g_ctx.initialized || g_ctx.sock_fd <= 0) return NULL;
        send_line(&g_ctx, line, (size_t)n);
    }
    return NULL;
}

static void start_heartbeat_thread(void) {
    if (heartbeat_started) return;
    if (pthread_create(&heartbeat_tid, NULL, heartbeat_loop, NULL) != 0) return;
    pthread_detach(heartbeat_tid);
    heartbeat_started = true;
}

void ara_emit_policy_hit(ara_ctx_t *ctx,
                         const char *op,
                         const char *path,
                         const char *raw_path,
                         const char *verdict,
                         const char *reason,
                         const char *rule_id) {
    if (!ctx || !ctx->initialized) return;
    char *e_op = json_escape(op ? op : "");
    char *e_path = json_escape(path ? path : "");
    char *e_raw = json_escape(raw_path ? raw_path : "");
    char *e_verdict = json_escape(verdict ? verdict : "");
    char *e_reason = json_escape(reason ? reason : "");
    char *e_rule = json_escape(rule_id ? rule_id : "");
    char buf[8192];
    int n = snprintf(buf, sizeof(buf),
                     "{\"type\":\"event\",\"kind\":\"policy_hit\","
                     "\"op\":\"%s\",\"path\":\"%s\",\"raw_path\":\"%s\","
                     "\"verdict\":\"%s\",\"reason\":\"%s\",\"rule_id\":\"%s\","
                     "\"pid\":%d,\"source\":\"libc-shim\"}\n",
                     e_op, e_path, e_raw, e_verdict, e_reason, e_rule, (int)getpid());
    if (n > 0) send_line(ctx, buf, (size_t)n);

    // For denies, the calling process is about to exit. Make sure the event
    // has actually reached the supervisor before we return: half-close our
    // write side and block briefly waiting for the supervisor to EOF the
    // read side, which it only does after draining pending events.
    if (verdict && strcmp(verdict, "deny") == 0 && ctx->sock_fd > 0) {
        shutdown(ctx->sock_fd, SHUT_WR);
        // Wait up to 200ms for supervisor's read loop to consume everything.
        // This is bounded so a stuck supervisor doesn't hang the process.
        for (int i = 0; i < 20; i++) {
            char sink[64];
            ssize_t r = recv(ctx->sock_fd, sink, sizeof(sink), MSG_DONTWAIT);
            if (r <= 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
                struct timespec ts = {0, 10 * 1000 * 1000};
                nanosleep(&ts, NULL);
                continue;
            }
            if (r <= 0) break;  // peer closed / error — done
        }
    }

    free(e_op); free(e_path); free(e_raw); free(e_verdict); free(e_reason); free(e_rule);
}

void ara_emit_spawn_event(ara_ctx_t *ctx, const char *command, char *const argv[]) {
    if (!ctx || !ctx->initialized) return;
    char *e_cmd = json_escape(command ? command : "");

    // Build "args":["a","b",...]
    char args_buf[4096] = "[";
    size_t off = 1;
    if (argv) {
        for (size_t i = 0; argv[i] && i < 32; i++) {
            char *esc = json_escape(argv[i]);
            int w = snprintf(args_buf + off, sizeof(args_buf) - off,
                             "%s\"%s\"", i == 0 ? "" : ",", esc ? esc : "");
            free(esc);
            if (w < 0) break;
            off += (size_t)w;
            if (off >= sizeof(args_buf) - 2) break;
        }
    }
    if (off < sizeof(args_buf) - 1) args_buf[off++] = ']';
    args_buf[off] = '\0';

    char buf[8192];
    int n = snprintf(buf, sizeof(buf),
                     "{\"type\":\"event\",\"kind\":\"spawn\","
                     "\"command\":\"%s\",\"args\":%s,\"pid\":%d,"
                     "\"source\":\"libc-shim\"}\n",
                     e_cmd, args_buf, (int)getpid());
    if (n > 0) send_line(ctx, buf, (size_t)n);
    free(e_cmd);
}
