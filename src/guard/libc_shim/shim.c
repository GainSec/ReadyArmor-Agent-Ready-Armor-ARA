// ARA-GUARD libc shim.
//
// Preloaded via LD_PRELOAD into every supervised worker + subprocess.
// Hooks exec* and open/openat at the libc boundary, applies the same
// deny_path rules as the Node guard, and audits to the same supervisor.
//
// Fully userspace, same uid as the worker, no kernel tricks. Designed
// to sit alongside the Node guard — Node-visible I/O goes through Node
// hooks; anything that slips past Node (shell-expanded paths, non-Node
// subprocess syscalls) is caught here.

#define _GNU_SOURCE
#define _LARGEFILE64_SOURCE

#include <dirent.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <netdb.h>
#include <unistd.h>
#include <stddef.h>

// Build a "<host>:<port>" string for audit-event targets.
static const char *target_from_host_port(const char *host, int port) {
    static __thread char buf[96];
    snprintf(buf, sizeof(buf), "%s:%d", host, port);
    return buf;
}

// Re-entry guard: when our hook is doing its own libc work (realpath,
// fopen on /proc, etc.), we must not re-enter the enforcement layer.
// Non-static so ipc.c can bump it during handshake setup.
__thread int ara_in_hook = 0;
#define ARA_ENTER() (ara_in_hook++)
#define ARA_EXIT()  (ara_in_hook--)
#define ARA_REENTRANT() (ara_in_hook > 0)

// Symbol strategy:
//   Linux — LD_PRELOAD + flat namespace: our `int connect(...)` directly
//           overrides libc's via RTLD_NEXT resolution.
//   macOS — two-level namespace: plain override is unreliable. Use the
//           canonical __DATA,__interpose section. We name our wrappers
//           `shim_<name>` and register {replacement, replacee} tuples at
//           the bottom of the file under __APPLE__.
#if defined(__APPLE__)
#  define SHIM_EXPORT(name) shim_##name
#  define DYLD_INTERPOSE(_replace, _replacee)                           \
     __attribute__((used)) static const struct {                        \
         const void *replace;                                           \
         const void *replacee;                                          \
     } _interpose_##_replacee                                           \
         __attribute__((section("__DATA,__interpose"))) = {             \
             (const void *)(unsigned long)&_replace,                    \
             (const void *)(unsigned long)&_replacee,                   \
     };
#else
#  define SHIM_EXPORT(name) name
#endif

#include "shim.h"

// Fail-closed gate.
//
// If the shim was loaded into a supervised worker (ARA_SOCKET set, or the
// supervisor explicitly requested enforcement via ARA_SHIM_REQUIRED=1) but
// never completed the handshake, we deny regulated ops instead of silently
// passing them through. Without this, any init-time failure — supervisor
// not listening, socket path wrong, token rejected — would blow a hole
// through the libc layer.
//
// Escape hatch: ARA_SHIM_FAILOPEN=1 disables fail-closed. Intended for
// dev/repair scenarios; the supervisor never sets it on production spawns.
static int ara_supervised_env(void) {
    const char *sock = getenv("ARA_SOCKET");
    const char *req  = getenv("ARA_SHIM_REQUIRED");
    if (sock && *sock) return 1;
    if (req && *req && strcmp(req, "0") != 0) return 1;
    return 0;
}

static int ara_failopen(void) {
    const char *v = getenv("ARA_SHIM_FAILOPEN");
    return v && *v && strcmp(v, "0") != 0;
}

// Returns 1 when the caller should deny with EACCES because the shim is
// not connected but should have been. Returns 0 to mean "pass through" —
// either the shim is connected (caller proceeds to real enforcement) or
// this process is legitimately unsupervised (no ARA_SOCKET).
static int ara_should_fail_closed(ara_ctx_t *ctx) {
    if (ctx && ctx->initialized) return 0;
    if (!ara_supervised_env()) return 0;
    if (ara_failopen()) return 0;
    return 1;
}

// Best-effort stderr warning; rate-limited once per process.
static void ara_warn_fail_closed_once(const char *op) {
    static int warned = 0;
    if (warned) return;
    warned = 1;
    fprintf(stderr,
        "[ara-shim] fail-closed: supervisor handshake incomplete, "
        "denying %s (pid=%d). Set ARA_SHIM_FAILOPEN=1 to override.\n",
        op ? op : "op", (int)getpid());
}

static int ara_bootstrap_entropy_path(const char *pathname) {
    if (!pathname) return 0;
    return strcmp(pathname, "/dev/urandom") == 0
        || strcmp(pathname, "/dev/random") == 0;
}

// ---- originals resolved via RTLD_NEXT ----

typedef int (*real_execve_t)(const char *, char *const[], char *const[]);
typedef int (*real_execv_t)(const char *, char *const[]);
typedef int (*real_execvp_t)(const char *, char *const[]);
typedef int (*real_execvpe_t)(const char *, char *const[], char *const[]);
typedef int (*real_openat_t)(int, const char *, int, ...);
typedef int (*real_open_t)(const char *, int, ...);
#if !defined(__APPLE__)
typedef int (*real_openat64_t)(int, const char *, int, ...);
typedef int (*real_open64_t)(const char *, int, ...);
typedef FILE *(*real_fopen64_t)(const char *, const char *);
typedef int (*real_stat64_t)(const char *, struct stat64 *);
typedef int (*real_lstat64_t)(const char *, struct stat64 *);
#endif
typedef DIR *(*real_opendir_t)(const char *);
typedef FILE *(*real_fopen_t)(const char *, const char *);
typedef int (*real_stat_t)(const char *, struct stat *);
typedef int (*real_lstat_t)(const char *, struct stat *);
typedef int (*real_access_t)(const char *, int);
typedef int (*real_faccessat_t)(int, const char *, int, int);
typedef int (*real_link_t)(const char *, const char *);
typedef int (*real_linkat_t)(int, const char *, int, const char *, int);
typedef int (*real_bind_t)(int, const struct sockaddr *, socklen_t);
typedef int (*real_connect_t)(int, const struct sockaddr *, socklen_t);
typedef int (*real_mkdir_t)(const char *, mode_t);
typedef int (*real_mkdirat_t)(int, const char *, mode_t);
typedef int (*real_rmdir_t)(const char *);
typedef int (*real_chdir_t)(const char *);
typedef int (*real_chmod_t)(const char *, mode_t);
typedef int (*real_fchmodat_t)(int, const char *, mode_t, int);
typedef int (*real_chown_t)(const char *, uid_t, gid_t);
typedef int (*real_lchown_t)(const char *, uid_t, gid_t);
typedef int (*real_fchownat_t)(int, const char *, uid_t, gid_t, int);
typedef int (*real_getaddrinfo_t)(const char *, const char *,
                                  const struct addrinfo *, struct addrinfo **);

static real_execve_t real_execve = NULL;
static real_execv_t real_execv = NULL;
static real_execvp_t real_execvp = NULL;
static real_execvpe_t real_execvpe = NULL;
static real_openat_t real_openat = NULL;
static real_open_t real_open = NULL;
#if !defined(__APPLE__)
static real_openat64_t real_openat64 = NULL;
static real_open64_t real_open64 = NULL;
static real_fopen64_t real_fopen64 = NULL;
static real_stat64_t real_stat64 = NULL;
static real_lstat64_t real_lstat64 = NULL;
#endif
static real_opendir_t real_opendir = NULL;
static real_fopen_t real_fopen = NULL;
static real_stat_t real_stat = NULL;
static real_lstat_t real_lstat = NULL;
static real_access_t real_access = NULL;
static real_faccessat_t real_faccessat = NULL;
static real_link_t real_link = NULL;
static real_linkat_t real_linkat = NULL;
static real_bind_t real_bind = NULL;
static real_connect_t real_connect = NULL;
static real_mkdir_t real_mkdir = NULL;
static real_mkdirat_t real_mkdirat = NULL;
static real_rmdir_t real_rmdir = NULL;
static real_chdir_t real_chdir = NULL;
static real_chmod_t real_chmod = NULL;
static real_fchmodat_t real_fchmodat = NULL;
static real_chown_t real_chown = NULL;
static real_lchown_t real_lchown = NULL;
static real_fchownat_t real_fchownat = NULL;
static real_getaddrinfo_t real_getaddrinfo = NULL;

#if defined(__APPLE__)
// On macOS the __DATA,__interpose registration redirects every external
// reference to e.g. `connect` (including dlsym(RTLD_NEXT)) back to our
// `shim_connect`, which would recurse forever when we tried to call the
// real one. Resolve explicitly through libSystem, which dyld does NOT
// interpose against us.
//
// Bootstrap hazard: dlopen() of libSystem itself can trigger internal
// open()/stat()/etc. which are interposed → back into resolve_originals
// → dlopen(...) → infinite recursion. We guard with a thread-local
// in-progress flag; during the initial resolve, subsequent interposed
// calls see a NULL handle, fall through to ARA_REENTRANT check in their
// wrapper, and short-circuit to the real implementation via the
// constructor's earlier resolution path.
static void *_ara_libsys(void) {
    static void *h = NULL;
    static __thread int in_progress = 0;
    if (h) return h;
    if (in_progress) return NULL;
    in_progress = 1;
    ARA_ENTER();  // mark all interposed ops reentrant during bootstrap
    h = dlopen("/usr/lib/libSystem.B.dylib", RTLD_LAZY | RTLD_LOCAL);
    ARA_EXIT();
    in_progress = 0;
    return h;
}
#  define ARA_RESOLVE(sym) dlsym(_ara_libsys(), sym)
#else
#  define ARA_RESOLVE(sym) dlsym(RTLD_NEXT, sym)
#endif

#if defined(__APPLE__)
// Direct addresses — inside this image, references to `connect` etc are
// NOT interposed (Apple's DYLD_INTERPOSE docs: interposition applies
// everywhere EXCEPT within the interposing image). So taking `&connect`
// here gives us libSystem's real connect address, which we stash.
extern int connect(int, const struct sockaddr *, socklen_t);
extern int bind(int, const struct sockaddr *, socklen_t);
extern int getaddrinfo(const char *, const char *,
                       const struct addrinfo *, struct addrinfo **);
extern int execve(const char *, char *const[], char *const[]);
extern int execv(const char *, char *const[]);
extern int execvp(const char *, char *const[]);
#endif

static void resolve_originals(void) {
#if defined(__APPLE__)
    // macOS phase-1 shortcut: for the interposed symbols, take the
    // direct in-image reference. These resolve to libSystem's real
    // function, not our shim. For uninterposed symbols, dlsym via
    // libSystem is still safe.
    if (!real_connect)     real_connect     = (real_connect_t)&connect;
    if (!real_bind)        real_bind        = (real_bind_t)&bind;
    if (!real_getaddrinfo) real_getaddrinfo = (real_getaddrinfo_t)&getaddrinfo;
    if (!real_execve)      real_execve      = (real_execve_t)&execve;
    if (!real_execv)       real_execv       = (real_execv_t)&execv;
    if (!real_execvp)      real_execvp      = (real_execvp_t)&execvp;
    // Remaining (non-interposed on macOS) symbols still via dlsym of libSystem:
    if (!real_openat) real_openat = ARA_RESOLVE("openat");
#else
    if (!real_execve) real_execve = ARA_RESOLVE("execve");
    if (!real_execv) real_execv = ARA_RESOLVE("execv");
    if (!real_execvp) real_execvp = ARA_RESOLVE("execvp");
    if (!real_execvpe) real_execvpe = ARA_RESOLVE("execvpe");
    if (!real_openat) real_openat = ARA_RESOLVE("openat");
    if (!real_open) real_open = ARA_RESOLVE("open");
#if !defined(__APPLE__)
    if (!real_openat64) real_openat64 = ARA_RESOLVE("openat64");
    if (!real_open64) real_open64 = ARA_RESOLVE("open64");
    if (!real_fopen64) real_fopen64 = ARA_RESOLVE("fopen64");
    if (!real_stat64) real_stat64 = ARA_RESOLVE("stat64");
    if (!real_lstat64) real_lstat64 = ARA_RESOLVE("lstat64");
#endif
    if (!real_opendir) real_opendir = ARA_RESOLVE("opendir");
    if (!real_fopen) real_fopen = ARA_RESOLVE("fopen");
    if (!real_stat) real_stat = ARA_RESOLVE("stat");
    if (!real_lstat) real_lstat = ARA_RESOLVE("lstat");
    if (!real_access) real_access = ARA_RESOLVE("access");
    if (!real_faccessat) real_faccessat = ARA_RESOLVE("faccessat");
    if (!real_link) real_link = ARA_RESOLVE("link");
    if (!real_linkat) real_linkat = ARA_RESOLVE("linkat");
    if (!real_bind) real_bind = ARA_RESOLVE("bind");
    if (!real_connect) real_connect = ARA_RESOLVE("connect");
    if (!real_mkdir) real_mkdir = ARA_RESOLVE("mkdir");
    if (!real_mkdirat) real_mkdirat = ARA_RESOLVE("mkdirat");
    if (!real_rmdir) real_rmdir = ARA_RESOLVE("rmdir");
    if (!real_chdir) real_chdir = ARA_RESOLVE("chdir");
    if (!real_chmod) real_chmod = ARA_RESOLVE("chmod");
    if (!real_fchmodat) real_fchmodat = ARA_RESOLVE("fchmodat");
    if (!real_chown) real_chown = ARA_RESOLVE("chown");
    if (!real_lchown) real_lchown = ARA_RESOLVE("lchown");
    if (!real_fchownat) real_fchownat = ARA_RESOLVE("fchownat");
    if (!real_getaddrinfo) real_getaddrinfo = ARA_RESOLVE("getaddrinfo");
#endif
}

__attribute__((constructor))
static void ara_shim_ctor(void) {
    resolve_originals();
    ARA_ENTER();
    if (getenv("ARA_SHIM_DEBUG")) {
        fprintf(stderr, "[ara-shim] ctor pid=%d ppid=%d ARA_SOCKET=%s\n",
                (int)getpid(), (int)getppid(),
                getenv("ARA_SOCKET") ? getenv("ARA_SOCKET") : "(unset)");
    }
    ara_ctx_t *ctx = ara_ctx();
    if (getenv("ARA_SHIM_DEBUG")) {
        fprintf(stderr, "[ara-shim] ctor init=%d rules=%zu\n",
                ctx ? ctx->initialized : 0,
                ctx ? ctx->rules.path_count : 0);
    }
    ARA_EXIT();
}

// ---- LD_PRELOAD heredity: make sure every exec keeps our shim alive ----

static char *join_with_space(const char *a, const char *b) {
    size_t la = strlen(a), lb = strlen(b);
    char *out = malloc(la + 1 + lb + 1);
    if (!out) return NULL;
    memcpy(out, a, la);
    out[la] = ' ';
    memcpy(out + la + 1, b, lb);
    out[la + 1 + lb] = '\0';
    return out;
}

static char **clone_envp_with_shim(char *const envp[]) {
    // Count existing entries.
    size_t n = 0;
    if (envp) {
        while (envp[n]) n += 1;
    }
    char **copy = malloc((n + 2) * sizeof(char *));
    if (!copy) return NULL;

    ara_ctx_t *ctx = ara_ctx();
    const char *shim_path = ctx && ctx->shim_path ? ctx->shim_path : NULL;

    bool ld_preload_seen = false;
    size_t out_i = 0;
    for (size_t i = 0; i < n; i++) {
        if (strncmp(envp[i], "LD_PRELOAD=", 11) == 0 && shim_path) {
            const char *existing = envp[i] + 11;
            if (strstr(existing, shim_path)) {
                copy[out_i++] = strdup(envp[i]);
            } else {
                size_t len = strlen("LD_PRELOAD=") + strlen(shim_path) + 1 + strlen(existing) + 1;
                char *merged = malloc(len);
                snprintf(merged, len, "LD_PRELOAD=%s %s", shim_path, existing);
                copy[out_i++] = merged;
            }
            ld_preload_seen = true;
        } else {
            copy[out_i++] = strdup(envp[i]);
        }
    }
    if (!ld_preload_seen && shim_path) {
        size_t len = strlen("LD_PRELOAD=") + strlen(shim_path) + 1;
        char *s = malloc(len);
        snprintf(s, len, "LD_PRELOAD=%s", shim_path);
        copy[out_i++] = s;
    }
    copy[out_i] = NULL;
    return copy;
}

static void free_env_copy(char **copy) {
    if (!copy) return;
    for (size_t i = 0; copy[i]; i++) free(copy[i]);
    free(copy);
}

static int env_truthy(const char *name) {
    const char *v = getenv(name);
    if (!v || !*v) return 0;
    return strcmp(v, "0") != 0 && strcmp(v, "false") != 0 && strcmp(v, "False") != 0;
}

static int channel_gate_budget(void) {
    const char *v = getenv("ARA_TASK_CHANNEL_EXEC_BUDGET");
    if (!v || !*v) return 25;
    char *endp = NULL;
    long n = strtol(v, &endp, 10);
    if (endp == v || n < 1 || n > 1000) return 25;
    return (int)n;
}

static int line_is_accepted_knowledge_channel(const char *line) {
    if (!line) return 0;
    if (strstr(line, "\"status\": \"rejected\"")) return 0;
    if (strstr(line, "\"channel\": \"fact.assert\"")) return 1;
    if (strstr(line, "\"channel\": \"lead.file\"")) return 1;
    if (strstr(line, "\"channel\": \"finding.file\"")) return 1;
    if (strstr(line, "\"kind\": \"channel.fact.assert\"")) return 1;
    if (strstr(line, "\"kind\": \"channel.lead.file\"")) return 1;
    if (strstr(line, "\"kind\": \"channel.finding.file\"")) return 1;
    return 0;
}

static int line_is_spawn_event(const char *line) {
    return line && strstr(line, "\"kind\": \"spawn\"") != NULL;
}

static int channel_gate_allows_exec(int *prior_spawns, int *accepted_channels) {
    if (prior_spawns) *prior_spawns = 0;
    if (accepted_channels) *accepted_channels = 0;
    if (!env_truthy("ARA_TASK_CHANNEL_REQUIRED")) return 1;

    const char *root = getenv("ARA_SESSION_ROOT");
    if (!root || !*root) return 1;

    char path[4096];
    int n = snprintf(path, sizeof(path), "%s/events.jsonl", root);
    if (n <= 0 || (size_t)n >= sizeof(path)) return 1;

    FILE *f = fopen(path, "r");
    if (!f) return 1;

    char buf[8192];
    int spawns = 0;
    int channels = 0;
    while (fgets(buf, sizeof(buf), f)) {
        if (line_is_spawn_event(buf)) spawns += 1;
        if (line_is_accepted_knowledge_channel(buf)) channels += 1;
    }
    fclose(f);

    if (prior_spawns) *prior_spawns = spawns;
    if (accepted_channels) *accepted_channels = channels;

    int budget = channel_gate_budget();
    int allowed = budget * (channels + 1);
    return spawns < allowed;
}

// ---- exec* family ----

static int enforce_exec(const char *pathname, char *const argv[]) {
    if (ARA_REENTRANT()) return 0;
    ara_ctx_t *ctx = ara_ctx();
    if (ara_should_fail_closed(ctx)) {
        ara_warn_fail_closed_once("exec.spawn");
        errno = EACCES;
        return -1;
    }
    if (!ctx || !ctx->initialized) return 0;

    ARA_ENTER();
    char *canon_bin = ara_canonicalize(pathname, NULL);
    int channel_gate_ok = 1;
    const char *task_supervisor = getenv("ARA_TASK_SUPERVISOR");
    const char *exec_path = canon_bin ? canon_bin : pathname;
    const char *exec_base = strrchr(exec_path, '/');
    exec_base = exec_base ? exec_base + 1 : exec_path;
    int task_loop_bookkeeping = 0;
    if (task_supervisor && strcmp(task_supervisor, "armory-hermes-loop") == 0 && exec_base) {
        if (strcmp(exec_base, "sleep") == 0 ||
            strcmp(exec_base, "date") == 0 ||
            strcmp(exec_base, "timeout") == 0) {
            task_loop_bookkeeping = 1;
        } else if (strcmp(exec_base, "hermes") == 0 && argv) {
            int has_continue = 0;
            int has_task_marker = 0;
            int has_commit_retry_prompt = 0;
            for (int i = 1; argv[i]; i++) {
                if (strcmp(argv[i], "--continue") == 0) {
                    has_continue = 1;
                }
                if (strncmp(argv[i], "ara-task-", 9) == 0) {
                    has_task_marker = 1;
                }
                if (strncmp(argv[i], "COMMIT_REQUIRED. You have taken a target action", 47) == 0) {
                    has_commit_retry_prompt = 1;
                }
            }
            if ((has_continue && has_task_marker) || has_commit_retry_prompt) {
                task_loop_bookkeeping = 1;
            }
        } else if ((strcmp(exec_base, "python3.9") == 0 ||
                    strcmp(exec_base, "python3") == 0 ||
                    strcmp(exec_base, "python") == 0) &&
                   argv && argv[1] && strcmp(argv[1], "-m") == 0 &&
                   argv[2] && strcmp(argv[2], "ara.cli") == 0 &&
                   argv[3] && strcmp(argv[3], "task-repair-commit") == 0 &&
                   argv[4] == NULL) {
            task_loop_bookkeeping = 1;
        } else if ((strcmp(exec_base, "python3.9") == 0 ||
                    strcmp(exec_base, "python3") == 0 ||
                    strcmp(exec_base, "python") == 0) &&
                   argv && argv[1] && strcmp(argv[1], "-c") == 0 &&
	                   argv[2] && strstr(argv[2], "ARA_STATE_ROOT") &&
	                   strstr(argv[2], "events.jsonl") &&
	                   strstr(argv[2], "channel.")) {
	            task_loop_bookkeeping = 1;
	        } else if ((strcmp(exec_base, "python3.9") == 0 ||
	                    strcmp(exec_base, "python3") == 0 ||
	                    strcmp(exec_base, "python") == 0) &&
	                   argv && argv[1] && strcmp(argv[1], "-c") == 0 &&
	                   argv[2] && strstr(argv[2], "ARA_STATE_ROOT") &&
	                   strstr(argv[2], "task-runs") &&
	                   strstr(argv[2], "session.json") &&
	                   strstr(argv[2], "phase")) {
	            task_loop_bookkeeping = 1;
	        }
	    }
    if (!task_loop_bookkeeping) {
        channel_gate_ok = channel_gate_allows_exec(NULL, NULL);
    }
    ARA_EXIT();

    if (!channel_gate_ok) {
        ARA_ENTER();
        ara_emit_policy_hit(ctx, "exec.spawn",
                            canon_bin ? canon_bin : pathname, pathname,
                            "deny", "ARA_CHANNEL_DEBT", "task_channel_gate");
        ARA_EXIT();
        free(canon_bin);
        errno = EACCES;
        return -1;
    }

    ARA_ENTER();
    ara_emit_spawn_event(ctx, pathname, argv);
    ARA_EXIT();

    const char *cmd_hit = ara_match_command(ctx, pathname, argv);
    if (cmd_hit) {
        ARA_ENTER();
        if (ctx->shadow_mode) {
            ara_emit_policy_hit(ctx, "exec.spawn",
                                canon_bin ? canon_bin : pathname, pathname,
                                "allow", "SHADOW_WOULD_DENY", cmd_hit);
        } else {
            ara_emit_policy_hit(ctx, "exec.spawn",
                                canon_bin ? canon_bin : pathname, pathname,
                                "deny", "DENY_COMMAND_BLOCKED", cmd_hit);
        }
        ARA_EXIT();
        if (!ctx->shadow_mode) {
            free(canon_bin);
            errno = EACCES;
            return -1;
        }
    }

    const char *hit = canon_bin ? ara_match_path(ctx, canon_bin) : NULL;
    if (hit) {
        ARA_ENTER();
        if (ctx->shadow_mode) {
            ara_emit_policy_hit(ctx, "exec.spawn", canon_bin, pathname,
                                "allow", "SHADOW_WOULD_DENY", hit);
        } else {
            ara_emit_policy_hit(ctx, "exec.spawn", canon_bin, pathname,
                                "deny", "DENY_SPAWN_TOUCHES_BLOCKED_PATH", hit);
        }
        ARA_EXIT();
        if (!ctx->shadow_mode) {
            free(canon_bin);
            errno = EACCES;
            return -1;
        }
    }

    if (argv) {
        for (size_t i = 0; argv[i]; i++) {
            const char *a = argv[i];
            if (!a || !*a) continue;
            bool pathish = false;
            if (a[0] == '/' || a[0] == '~' ||
                (a[0] == '.' && (a[1] == '/' || (a[1] == '.' && a[2] == '/')))) {
                pathish = true;
            } else if (strchr(a, '/') && a[0] != '-') {
                pathish = true;
            }
            if (!pathish) continue;
            ARA_ENTER();
            char *canon = ara_canonicalize(a, NULL);
            ARA_EXIT();
            if (!canon) continue;
            const char *h = ara_match_path(ctx, canon);
            if (h) {
                ARA_ENTER();
                if (ctx->shadow_mode) {
                    ara_emit_policy_hit(ctx, "exec.spawn", canon, a,
                                        "allow", "SHADOW_WOULD_DENY", h);
                } else {
                    ara_emit_policy_hit(ctx, "exec.spawn", canon, a,
                                        "deny", "DENY_SPAWN_TOUCHES_BLOCKED_PATH", h);
                }
                ARA_EXIT();
                if (!ctx->shadow_mode) {
                    free(canon);
                    free(canon_bin);
                    errno = EACCES;
                    return -1;
                }
            }
            free(canon);
        }
    }

    if (canon_bin) free(canon_bin);
    return 0;
}

int SHIM_EXPORT(execve)(const char *pathname, char *const argv[], char *const envp[]) {
    resolve_originals();
    if (enforce_exec(pathname, argv) < 0) return -1;
    char **env_with_shim = clone_envp_with_shim(envp);
    int rc = real_execve(pathname, argv, env_with_shim ? env_with_shim : envp);
    // execve only returns on failure.
    free_env_copy(env_with_shim);
    return rc;
}

int SHIM_EXPORT(execv)(const char *pathname, char *const argv[]) {
    resolve_originals();
    if (enforce_exec(pathname, argv) < 0) return -1;
    // execv uses the caller's environ; we can't rewrite it, but LD_PRELOAD
    // should already be set in the current env, so descendants inherit it.
    return real_execv(pathname, argv);
}

int SHIM_EXPORT(execvp)(const char *file, char *const argv[]) {
    resolve_originals();
    if (enforce_exec(file, argv) < 0) return -1;
    return real_execvp(file, argv);
}

int SHIM_EXPORT(execvpe)(const char *file, char *const argv[], char *const envp[]) {
    resolve_originals();
    if (enforce_exec(file, argv) < 0) return -1;
    char **env_with_shim = clone_envp_with_shim(envp);
    int rc = real_execvpe(file, argv, env_with_shim ? env_with_shim : envp);
    free_env_copy(env_with_shim);
    return rc;
}

// execl/execlp/execle are usually implemented via exec[vp]e under the hood
// in glibc; hooking the v-form family is sufficient to cover them.

// ---- open / openat ----

static int enforce_open(const char *pathname) {
    if (ARA_REENTRANT()) return 0;
    ara_ctx_t *ctx = ara_ctx();
    if (ara_bootstrap_entropy_path(pathname)) return 0;
    if (ara_should_fail_closed(ctx)) {
        ara_warn_fail_closed_once("fs.open");
        errno = EACCES;
        return -1;
    }
    if (!ctx || !ctx->initialized) return 0;
    ARA_ENTER();
    char *canon = ara_canonicalize(pathname, NULL);
    ARA_EXIT();
    if (!canon) return 0;
    if (getenv("ARA_SHIM_DEBUG")) {
        fprintf(stderr, "[ara-shim] open pathname=%s canon=%s\n", pathname, canon);
    }
    const char *hit = ara_match_path(ctx, canon);
    if (hit) {
        ARA_ENTER();
        if (ctx->shadow_mode) {
            ara_emit_policy_hit(ctx, "fs.open", canon, pathname,
                                "allow", "SHADOW_WOULD_DENY", hit);
        } else {
            ara_emit_policy_hit(ctx, "fs.open", canon, pathname,
                                "deny", "DENY_FILE_PATH_BLOCKED", hit);
        }
        ARA_EXIT();
        if (!ctx->shadow_mode) {
            free(canon);
            errno = EACCES;
            return -1;
        }
    }
    free(canon);
    return 0;
}

static const char *resolve_openat_path(
    int dirfd,
    const char *pathname,
    char *resolved,
    size_t resolved_len
) {
    const char *check_path = pathname;
    if (pathname && pathname[0] != '/') {
        if (dirfd == AT_FDCWD) {
            char cwd[4096];
            if (getcwd(cwd, sizeof(cwd))) {
                snprintf(resolved, resolved_len, "%s/%s", cwd, pathname);
                check_path = resolved;
            }
        } else {
            char link[64];
            snprintf(link, sizeof(link), "/proc/self/fd/%d", dirfd);
            char dirpath[4096];
            ssize_t n = readlink(link, dirpath, sizeof(dirpath) - 1);
            if (n > 0) {
                dirpath[n] = '\0';
                snprintf(resolved, resolved_len, "%s/%s", dirpath, pathname);
                check_path = resolved;
            }
        }
    }
    return check_path;
}

int SHIM_EXPORT(openat)(int dirfd, const char *pathname, int flags, ...) {
    resolve_originals();

    char resolved[4096];
    const char *check_path = resolve_openat_path(
        dirfd, pathname, resolved, sizeof(resolved)
    );
    if (enforce_open(check_path) < 0) return -1;

    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list ap;
        va_start(ap, flags);
        mode = va_arg(ap, mode_t);
        va_end(ap);
    }
    return real_openat(dirfd, pathname, flags, mode);
}

int SHIM_EXPORT(open)(const char *pathname, int flags, ...) {
    resolve_originals();
    if (enforce_open(pathname) < 0) return -1;
    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list ap;
        va_start(ap, flags);
        mode = va_arg(ap, mode_t);
        va_end(ap);
    }
    return real_open(pathname, flags, mode);
}

#if !defined(__APPLE__)
int openat64(int dirfd, const char *pathname, int flags, ...) {
    resolve_originals();

    char resolved[4096];
    const char *check_path = resolve_openat_path(
        dirfd, pathname, resolved, sizeof(resolved)
    );
    if (enforce_open(check_path) < 0) return -1;

    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list ap;
        va_start(ap, flags);
        mode = va_arg(ap, mode_t);
        va_end(ap);
    }
    return real_openat64 ? real_openat64(dirfd, pathname, flags, mode)
                         : real_openat(dirfd, pathname, flags, mode);
}

int open64(const char *pathname, int flags, ...) {
    resolve_originals();
    if (enforce_open(pathname) < 0) return -1;
    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list ap;
        va_start(ap, flags);
        mode = va_arg(ap, mode_t);
        va_end(ap);
    }
    return real_open64 ? real_open64(pathname, flags, mode)
                       : real_open(pathname, flags, mode);
}
#endif

// Higher-level path-accepting libc functions. We hook these too because
// glibc's internal callers (opendir, fopen, stat, etc.) don't go through
// the symbol resolution boundary when they call openat / open internally,
// so our openat hook alone doesn't cover `ls`, `cat`, `head`, etc.

DIR *opendir(const char *name) {
    resolve_originals();
    if (enforce_open(name) < 0) return NULL;
    return real_opendir ? real_opendir(name) : NULL;
}

FILE *fopen(const char *pathname, const char *mode) {
    resolve_originals();
    if (enforce_open(pathname) < 0) return NULL;
    return real_fopen ? real_fopen(pathname, mode) : NULL;
}

#if !defined(__APPLE__)
FILE *fopen64(const char *pathname, const char *mode) {
    resolve_originals();
    if (enforce_open(pathname) < 0) return NULL;
    return real_fopen64 ? real_fopen64(pathname, mode) : real_fopen(pathname, mode);
}
#endif

int SHIM_EXPORT(stat)(const char *pathname, struct stat *statbuf) {
    resolve_originals();
    if (enforce_open(pathname) < 0) return -1;
    return real_stat ? real_stat(pathname, statbuf) : -1;
}

int SHIM_EXPORT(lstat)(const char *pathname, struct stat *statbuf) {
    resolve_originals();
    if (enforce_open(pathname) < 0) return -1;
    return real_lstat ? real_lstat(pathname, statbuf) : -1;
}

#if !defined(__APPLE__)
int stat64(const char *pathname, struct stat64 *statbuf) {
    resolve_originals();
    if (enforce_open(pathname) < 0) return -1;
    return real_stat64 ? real_stat64(pathname, statbuf) : -1;
}

int lstat64(const char *pathname, struct stat64 *statbuf) {
    resolve_originals();
    if (enforce_open(pathname) < 0) return -1;
    return real_lstat64 ? real_lstat64(pathname, statbuf) : -1;
}
#endif

int SHIM_EXPORT(access)(const char *pathname, int mode) {
    resolve_originals();
    if (enforce_open(pathname) < 0) return -1;
    return real_access ? real_access(pathname, mode) : -1;
}

int SHIM_EXPORT(faccessat)(int dirfd, const char *pathname, int mode, int flags) {
    resolve_originals();
    if (enforce_open(pathname) < 0) return -1;
    return real_faccessat ? real_faccessat(dirfd, pathname, mode, flags) : -1;
}

// ---- link / linkat: block hardlinking a walled source into a cheap alias. ----
//
// The dangerous half is the SOURCE path — if we let the agent link
// `~/.ssh/id_rsa` to `/tmp/decoy`, a later open of `/tmp/decoy` would be
// indistinguishable from any normal open and wouldn't trigger the path
// rule. So we match the source against the same deny list `open()` uses.

static int enforce_link(const char *src_path) {
    if (ARA_REENTRANT()) return 0;
    ara_ctx_t *ctx = ara_ctx();
    if (ara_should_fail_closed(ctx)) {
        ara_warn_fail_closed_once("fs.link");
        errno = EACCES;
        return -1;
    }
    if (!ctx || !ctx->initialized) return 0;
    ARA_ENTER();
    char *canon = ara_canonicalize(src_path, NULL);
    ARA_EXIT();
    if (!canon) return 0;
    const char *hit = ara_match_path(ctx, canon);
    if (hit) {
        ARA_ENTER();
        if (ctx->shadow_mode) {
            ara_emit_policy_hit(ctx, "fs.link", canon, src_path,
                                "allow", "SHADOW_WOULD_DENY", hit);
        } else {
            ara_emit_policy_hit(ctx, "fs.link", canon, src_path,
                                "deny", "DENY_LINK_SOURCE_BLOCKED", hit);
        }
        ARA_EXIT();
        if (!ctx->shadow_mode) {
            free(canon);
            errno = EACCES;
            return -1;
        }
    }
    free(canon);
    return 0;
}

int SHIM_EXPORT(link)(const char *oldpath, const char *newpath) {
    resolve_originals();
    if (enforce_link(oldpath) < 0) return -1;
    return real_link ? real_link(oldpath, newpath) : -1;
}

int SHIM_EXPORT(linkat)(int olddirfd, const char *oldpath,
           int newdirfd, const char *newpath, int flags) {
    resolve_originals();
    // Resolve relative oldpath against olddirfd, same pattern as openat.
    char resolved[4096];
    const char *check = oldpath;
    if (oldpath && oldpath[0] != '/') {
        if (olddirfd == AT_FDCWD) {
            char cwd[4096];
            if (getcwd(cwd, sizeof(cwd))) {
                snprintf(resolved, sizeof(resolved), "%s/%s", cwd, oldpath);
                check = resolved;
            }
        } else {
            char lnk[64];
            snprintf(lnk, sizeof(lnk), "/proc/self/fd/%d", olddirfd);
            char dirpath[4096];
            ssize_t n = readlink(lnk, dirpath, sizeof(dirpath) - 1);
            if (n > 0) {
                dirpath[n] = '\0';
                snprintf(resolved, sizeof(resolved), "%s/%s", dirpath, oldpath);
                check = resolved;
            }
        }
    }
    if (enforce_link(check) < 0) return -1;
    return real_linkat
        ? real_linkat(olddirfd, oldpath, newdirfd, newpath, flags)
        : -1;
}

// ---- bind: audit outbound / listening sockets. ----
//
// Audit-only for now (no enforcement rules for bind yet). Emits enough
// context for operators to see "worker tried to listen on :9999" — useful
// for spotting reverse shells or C2 beacons before we add explicit rules.

// ---- connect: enforce deny_net rules against IP/port. ----
//
// Note: hostname-based rules (e.g. ``*.openai.com``) are caught by the Node
// guard pre-DNS; by the time libc sees connect(), only the IP is left.
// So this hook enforces IP-literal patterns and audits everything else —
// SNI-from-ClientHello parsing is a separate follow-up.

int SHIM_EXPORT(connect)(int sockfd, const struct sockaddr *addr, socklen_t addrlen) {
    resolve_originals();
    if (getenv("ARA_SHIM_DEBUG"))
        fprintf(stderr, "[ara-shim] connect() intercepted fd=%d\n", sockfd);
    if (ARA_REENTRANT() || !addr) {
        return real_connect ? real_connect(sockfd, addr, addrlen) : -1;
    }
    ara_ctx_t *ctx = ara_ctx();
    if (ara_should_fail_closed(ctx)) {
        ara_warn_fail_closed_once("net.connect");
        errno = EACCES;
        return -1;
    }
    if (!ctx || !ctx->initialized) {
        return real_connect ? real_connect(sockfd, addr, addrlen) : -1;
    }

    // ---- invisible redirect: AF_UNIX openclaw exec-approvals socket ----
    //
    // If the worker connects to the real openclaw approvals socket, swap
    // the sockaddr to ARA's proxy socket path before the real connect.
    // The proxy parses JSONL, applies Bulwark exec rules, forwards to
    // the real socket if allowed. No config rewrite needed — covers
    // non-contained sessions too. Env:
    //   ARA_EXEC_APPROVALS_REAL_PATH — the socket path to redirect
    //   ARA_EXEC_APPROVALS_TAP_PATH  — the proxy's listen path
    if (addr->sa_family == AF_UNIX
        && addrlen >= offsetof(struct sockaddr_un, sun_path) + 2) {
        const char *real_p = getenv("ARA_EXEC_APPROVALS_REAL_PATH");
        const char *tap_p = getenv("ARA_EXEC_APPROVALS_TAP_PATH");
        if (real_p && tap_p) {
            const struct sockaddr_un *u = (const struct sockaddr_un *)addr;
            if (u->sun_path[0] != '\0'
                && strncmp(u->sun_path, real_p, sizeof(u->sun_path)) == 0) {
                struct sockaddr_un redirected;
                memset(&redirected, 0, sizeof(redirected));
                redirected.sun_family = AF_UNIX;
                size_t np = strnlen(tap_p, sizeof(redirected.sun_path) - 1);
                memcpy(redirected.sun_path, tap_p, np);
                redirected.sun_path[np] = '\0';
                socklen_t new_len =
                    (socklen_t)(offsetof(struct sockaddr_un, sun_path) + np + 1);
                ARA_ENTER();
                ara_emit_policy_hit(
                    ctx, "shim.redirect", tap_p, real_p,
                    "allow", "AF_UNIX_REDIRECT_EXEC_APPROVALS", NULL);
                ARA_EXIT();
                return real_connect
                    ? real_connect(sockfd, (const struct sockaddr *)&redirected, new_len)
                    : -1;
            }
        }
        // Other AF_UNIX destinations — audit-only, no enforcement.
        return real_connect ? real_connect(sockfd, addr, addrlen) : -1;
    }

    char host[INET6_ADDRSTRLEN] = "";
    int port = 0;
    if (addr->sa_family == AF_INET && addrlen >= sizeof(struct sockaddr_in)) {
        const struct sockaddr_in *a = (const struct sockaddr_in *)addr;
        inet_ntop(AF_INET, &a->sin_addr, host, sizeof(host));
        port = ntohs(a->sin_port);
    } else if (addr->sa_family == AF_INET6
               && addrlen >= sizeof(struct sockaddr_in6)) {
        const struct sockaddr_in6 *a = (const struct sockaddr_in6 *)addr;
        inet_ntop(AF_INET6, &a->sin6_addr, host, sizeof(host));
        port = ntohs(a->sin6_port);
    } else {
        // AF_UNIX etc. — not in net-rule scope.
        return real_connect ? real_connect(sockfd, addr, addrlen) : -1;
    }

    // ---- invisible redirect: AF_INET openclaw gateway port ----
    //
    // TCP connects to 127.0.0.1:<real-gateway-port> are rewritten to the
    // ARA WebSocket tap port. Worker talks to ARA without knowing it.
    // Env:
    //   ARA_GATEWAY_REAL_PORT — 18789 (or whatever the live gateway uses)
    //   ARA_GATEWAY_TAP_PORT  — the tap's ephemeral listen port
    const char *gw_real_s = getenv("ARA_GATEWAY_REAL_PORT");
    const char *gw_tap_s = getenv("ARA_GATEWAY_TAP_PORT");
    if (gw_real_s && gw_tap_s
        && (strcmp(host, "127.0.0.1") == 0 || strcmp(host, "::1") == 0)
        && port == atoi(gw_real_s)
        && atoi(gw_tap_s) > 0 && atoi(gw_tap_s) != port) {
        int new_port = atoi(gw_tap_s);
        if (addr->sa_family == AF_INET) {
            struct sockaddr_in redirected = *(const struct sockaddr_in *)addr;
            redirected.sin_port = htons((uint16_t)new_port);
            ARA_ENTER();
            char dst[64];
            snprintf(dst, sizeof(dst), "127.0.0.1:%d", new_port);
            ara_emit_policy_hit(
                ctx, "shim.redirect", dst, target_from_host_port(host, port),
                "allow", "AF_INET_REDIRECT_GATEWAY", NULL);
            ARA_EXIT();
            return real_connect
                ? real_connect(sockfd, (const struct sockaddr *)&redirected, sizeof(redirected))
                : -1;
        }
        if (addr->sa_family == AF_INET6) {
            struct sockaddr_in6 redirected = *(const struct sockaddr_in6 *)addr;
            redirected.sin6_port = htons((uint16_t)new_port);
            ARA_ENTER();
            char dst[64];
            snprintf(dst, sizeof(dst), "::1:%d", new_port);
            ara_emit_policy_hit(
                ctx, "shim.redirect", dst, target_from_host_port(host, port),
                "allow", "AF_INET_REDIRECT_GATEWAY", NULL);
            ARA_EXIT();
            return real_connect
                ? real_connect(sockfd, (const struct sockaddr *)&redirected, sizeof(redirected))
                : -1;
        }
    }

    // Our own supervisor-socket connect goes through AF_UNIX; never reaches
    // here. Still, guard against connecting to loopback if a rule blindly
    // matched (rules are trust-the-operator so we don't special-case).
    char target[64];
    snprintf(target, sizeof(target), "%s:%d", host, port);
    const char *allow_hit = ara_match_allow_net(ctx, host, port);
    if (allow_hit) {
        ARA_ENTER();
        ara_emit_policy_hit(ctx, "net.connect", target, target,
                            "allow", "ALLOW_NET_ALLOWLIST", allow_hit);
        ARA_EXIT();
        return real_connect ? real_connect(sockfd, addr, addrlen) : -1;
    }

    const char *hit = ara_match_net(ctx, host, port);
    if (hit) {
        ARA_ENTER();
        if (ctx->shadow_mode) {
            ara_emit_policy_hit(ctx, "net.connect", target, target,
                                "allow", "SHADOW_WOULD_DENY", hit);
        } else {
            ara_emit_policy_hit(ctx, "net.connect", target, target,
                                "deny", "DENY_NET_BLOCKED", hit);
        }
        ARA_EXIT();
        if (!ctx->shadow_mode) {
            errno = EACCES;
            return -1;
        }
    } else {
        // Default-deny hardening (belt-and-suspenders). A connect() that
        // matched neither the allowlist nor an explicit deny rule is, by
        // default, only AUDIT_ONLY-allowed — which means a missing or
        // partial net ruleset silently leaks egress to arbitrary hosts
        // (the direct-socket bypass). When ARA_SHIM_NET_ENFORCE is set the
        // fallback flips to DENY, so the shim fails SAFE. Gated (opt-in per
        // contain-profile) so session types whose net allowlist isn't yet
        // complete keep legacy behavior until migrated; enable it for the
        // pentest task profile, whose trim now emits a full allowlist +
        // `deny net *:*`.
        static int net_enforce = -1;
        if (net_enforce < 0) {
            const char *e = getenv("ARA_SHIM_NET_ENFORCE");
            net_enforce = (e && *e && strcmp(e, "0") != 0) ? 1 : 0;
        }
        if (net_enforce && !ctx->shadow_mode) {
            ARA_ENTER();
            ara_emit_policy_hit(ctx, "net.connect", target, target,
                                "deny", "DENY_NET_DEFAULT", NULL);
            ARA_EXIT();
            errno = EACCES;
            return -1;
        }
        ARA_ENTER();
        ara_emit_policy_hit(ctx, "net.connect", target, target, "allow",
                            net_enforce ? "SHADOW_WOULD_DENY_DEFAULT"
                                        : "AUDIT_ONLY", NULL);
        ARA_EXIT();
    }
    return real_connect ? real_connect(sockfd, addr, addrlen) : -1;
}

int SHIM_EXPORT(bind)(int sockfd, const struct sockaddr *addr, socklen_t addrlen) {
    resolve_originals();
    ara_ctx_t *ctx = ara_ctx();
    // Don't emit during reentrancy or before initialization — avoids
    // lighting up the audit log from our own ipc.c handshake socket.
    if (!ARA_REENTRANT() && ctx && ctx->initialized && addr) {
        char target[128] = "";
        if (addr->sa_family == AF_INET && addrlen >= sizeof(struct sockaddr_in)) {
            const struct sockaddr_in *a = (const struct sockaddr_in *)addr;
            char buf[INET_ADDRSTRLEN] = "";
            inet_ntop(AF_INET, &a->sin_addr, buf, sizeof(buf));
            snprintf(target, sizeof(target), "%s:%d", buf, ntohs(a->sin_port));
        } else if (addr->sa_family == AF_INET6
                   && addrlen >= sizeof(struct sockaddr_in6)) {
            const struct sockaddr_in6 *a = (const struct sockaddr_in6 *)addr;
            char buf[INET6_ADDRSTRLEN] = "";
            inet_ntop(AF_INET6, &a->sin6_addr, buf, sizeof(buf));
            snprintf(target, sizeof(target), "[%s]:%d", buf, ntohs(a->sin6_port));
        } else if (addr->sa_family == AF_UNIX) {
            const struct sockaddr_un *a = (const struct sockaddr_un *)addr;
            snprintf(target, sizeof(target), "unix:%s", a->sun_path);
        }
        if (target[0]) {
            ARA_ENTER();
            ara_emit_policy_hit(ctx, "net.bind", target, target,
                                "allow", "AUDIT_ONLY", NULL);
            ARA_EXIT();
        }
    }
    return real_bind ? real_bind(sockfd, addr, addrlen) : -1;
}

// ---- nav + perm (Bulwark layers) -----------------------------------
// Generic enforcement helper: canonicalize, consult the rule set, emit
// policy_hit, return -1/EACCES if denied. ``matcher`` is one of
// ara_match_nav / ara_match_perm.

typedef const char *(*ara_matcher_t)(ara_ctx_t *, const char *);

static int enforce_layer(
    const char *op, const char *pathname, ara_matcher_t matcher
) {
    if (ARA_REENTRANT()) return 0;
    ara_ctx_t *ctx = ara_ctx();
    if (ara_should_fail_closed(ctx)) {
        ara_warn_fail_closed_once(op);
        errno = EACCES;
        return -1;
    }
    if (!ctx || !ctx->initialized || !pathname) return 0;
    ARA_ENTER();
    char *canon = ara_canonicalize(pathname, NULL);
    ARA_EXIT();
    if (!canon) return 0;
    const char *hit = matcher(ctx, canon);
    if (hit) {
        ARA_ENTER();
        if (ctx->shadow_mode) {
            ara_emit_policy_hit(ctx, op, canon, pathname,
                                "allow", "SHADOW_WOULD_DENY", hit);
        } else {
            ara_emit_policy_hit(ctx, op, canon, pathname,
                                "deny", "DENY_BULWARK_BLOCKED", hit);
        }
        ARA_EXIT();
        if (!ctx->shadow_mode) {
            free(canon);
            errno = EACCES;
            return -1;
        }
    }
    free(canon);
    return 0;
}

// Resolve an *at-flavour (dirfd + relative path) into an absolute path
// string we can pass to enforce_layer. Mirrors the logic used inside
// openat() above. Writes to ``out`` (size ``outsz``); returns a pointer
// into ``out`` on success, otherwise the original ``pathname``.
static const char *resolve_at(
    int dirfd, const char *pathname, char *out, size_t outsz
) {
    if (!pathname || pathname[0] == '/') return pathname;
    if (dirfd == AT_FDCWD) {
        char cwd[4096];
        if (getcwd(cwd, sizeof(cwd))) {
            snprintf(out, outsz, "%s/%s", cwd, pathname);
            return out;
        }
        return pathname;
    }
    char link[64];
    snprintf(link, sizeof(link), "/proc/self/fd/%d", dirfd);
    char dirpath[4096];
    ssize_t n = readlink(link, dirpath, sizeof(dirpath) - 1);
    if (n > 0) {
        dirpath[n] = '\0';
        snprintf(out, outsz, "%s/%s", dirpath, pathname);
        return out;
    }
    return pathname;
}

int SHIM_EXPORT(mkdir)(const char *pathname, mode_t mode) {
    resolve_originals();
    if (enforce_layer("fs.mkdir", pathname, ara_match_nav) < 0) return -1;
    return real_mkdir ? real_mkdir(pathname, mode) : -1;
}

int SHIM_EXPORT(mkdirat)(int dirfd, const char *pathname, mode_t mode) {
    resolve_originals();
    char resolved[4096];
    const char *cp = resolve_at(dirfd, pathname, resolved, sizeof(resolved));
    if (enforce_layer("fs.mkdir", cp, ara_match_nav) < 0) return -1;
    return real_mkdirat ? real_mkdirat(dirfd, pathname, mode) : -1;
}

int SHIM_EXPORT(rmdir)(const char *pathname) {
    resolve_originals();
    if (enforce_layer("fs.rmdir", pathname, ara_match_nav) < 0) return -1;
    return real_rmdir ? real_rmdir(pathname) : -1;
}

int SHIM_EXPORT(chdir)(const char *pathname) {
    resolve_originals();
    if (enforce_layer("fs.chdir", pathname, ara_match_nav) < 0) return -1;
    return real_chdir ? real_chdir(pathname) : -1;
}

int SHIM_EXPORT(chmod)(const char *pathname, mode_t mode) {
    resolve_originals();
    if (enforce_layer("fs.chmod", pathname, ara_match_perm) < 0) return -1;
    return real_chmod ? real_chmod(pathname, mode) : -1;
}

int SHIM_EXPORT(fchmodat)(int dirfd, const char *pathname, mode_t mode, int flags) {
    resolve_originals();
    char resolved[4096];
    const char *cp = resolve_at(dirfd, pathname, resolved, sizeof(resolved));
    if (enforce_layer("fs.chmod", cp, ara_match_perm) < 0) return -1;
    return real_fchmodat ? real_fchmodat(dirfd, pathname, mode, flags) : -1;
}

int SHIM_EXPORT(chown)(const char *pathname, uid_t uid, gid_t gid) {
    resolve_originals();
    if (enforce_layer("fs.chown", pathname, ara_match_perm) < 0) return -1;
    return real_chown ? real_chown(pathname, uid, gid) : -1;
}

int SHIM_EXPORT(lchown)(const char *pathname, uid_t uid, gid_t gid) {
    resolve_originals();
    if (enforce_layer("fs.chown", pathname, ara_match_perm) < 0) return -1;
    return real_lchown ? real_lchown(pathname, uid, gid) : -1;
}

int SHIM_EXPORT(fchownat)(int dirfd, const char *pathname, uid_t uid, gid_t gid, int flags) {
    resolve_originals();
    char resolved[4096];
    const char *cp = resolve_at(dirfd, pathname, resolved, sizeof(resolved));
    if (enforce_layer("fs.chown", cp, ara_match_perm) < 0) return -1;
    return real_fchownat ? real_fchownat(dirfd, pathname, uid, gid, flags) : -1;
}

// ---- getaddrinfo: audit DNS + force-resolve redirected hostnames ----
//
// If the hostname appears in ARA_GATEWAY_HOSTNAMES (comma-separated), we
// synthesize a 127.0.0.1 result so the subsequent connect() lands on
// loopback — where the AF_INET redirect above rewrites the port to the
// tap. Everything else is passed through with an audit event.
//
// Format: ARA_GATEWAY_HOSTNAMES="gateway.local,openclaw.internal"
static int hostname_in_csv(const char *needle, const char *csv) {
    if (!needle || !csv || !*needle || !*csv) return 0;
    size_t nlen = strlen(needle);
    const char *p = csv;
    while (*p) {
        while (*p == ',' || *p == ' ') p++;
        const char *start = p;
        while (*p && *p != ',') p++;
        size_t len = (size_t)(p - start);
        if (len == nlen && strncasecmp(start, needle, nlen) == 0) return 1;
    }
    return 0;
}

int SHIM_EXPORT(getaddrinfo)(const char *node, const char *service,
                const struct addrinfo *hints, struct addrinfo **res) {
    resolve_originals();
    if (ARA_REENTRANT() || !real_getaddrinfo) {
        return real_getaddrinfo
            ? real_getaddrinfo(node, service, hints, res)
            : EAI_FAIL;
    }
    ara_ctx_t *ctx = ara_ctx();

    const char *csv = getenv("ARA_GATEWAY_HOSTNAMES");
    if (node && csv && hostname_in_csv(node, csv)) {
        // Build a single-entry AF_INET result pointing at 127.0.0.1.
        struct addrinfo *ai = calloc(1, sizeof(*ai));
        struct sockaddr_in *sa = calloc(1, sizeof(*sa));
        if (ai && sa) {
            sa->sin_family = AF_INET;
            sa->sin_addr.s_addr = htonl(INADDR_LOOPBACK);
            if (service && *service) {
                int p = atoi(service);
                if (p > 0 && p < 65536) sa->sin_port = htons((uint16_t)p);
            }
            ai->ai_family = AF_INET;
            ai->ai_socktype = hints ? hints->ai_socktype : SOCK_STREAM;
            ai->ai_protocol = hints ? hints->ai_protocol : 0;
            ai->ai_addrlen = sizeof(*sa);
            ai->ai_addr = (struct sockaddr *)sa;
            *res = ai;
            if (ctx && ctx->initialized) {
                ARA_ENTER();
                ara_emit_policy_hit(
                    ctx, "dns.resolve", node, "127.0.0.1",
                    "allow", "DNS_REDIRECT_LOOPBACK", NULL);
                ARA_EXIT();
            }
            return 0;
        }
        free(ai);
        free(sa);
        // Fall through to real resolver on allocation failure.
    }

    int rc = real_getaddrinfo(node, service, hints, res);
    if (ctx && ctx->initialized && node) {
        ARA_ENTER();
        ara_emit_policy_hit(
            ctx, "dns.resolve", node, node,
            rc == 0 ? "allow" : "deny",
            rc == 0 ? "AUDIT_ONLY" : "DNS_RESOLVE_FAIL", NULL);
        ARA_EXIT();
    }
    return rc;
}

// ---- macOS: register __DATA,__interpose entries for every wrapper ----
//
// On macOS dyld, symbol-override under DYLD_INSERT_LIBRARIES is unreliable
// (two-level namespace routes most libc calls directly to libSystem). The
// canonical fix is the interpose section: dyld rewrites every external
// reference to the target symbol to point at our replacement, without
// touching the caller image.
//
// Each entry must name (replacement, replacee). Declare extern forward
// decls for the replacees so we can take their address here — we don't
// include their headers again to avoid type mismatches.
#if defined(__APPLE__)
// macOS phase-1: interpose NETWORK and EXEC calls only. Interposing the
// fs layer (open/openat/stat/access/…) causes a bootstrap recursion —
// dlopen() inside our ctor internally opens file descriptors, those
// hit our interposed open, which re-enters resolve_originals → dlopen.
// Network and exec paths don't hit this: libSystem's loader doesn't
// call connect/bind/execve during our initialization.
//
// When we later need fs interposition on macOS, the fix is either
// (a) syscall-based fallback for open/stat/… when real_* is NULL, or
// (b) pre-resolve libSystem handle via a dyld priv API, bypassing the
// recursive open() path. Deferred.
DYLD_INTERPOSE(shim_connect,     connect)
DYLD_INTERPOSE(shim_bind,        bind)
DYLD_INTERPOSE(shim_getaddrinfo, getaddrinfo)
DYLD_INTERPOSE(shim_execve,      execve)
DYLD_INTERPOSE(shim_execv,       execv)
DYLD_INTERPOSE(shim_execvp,      execvp)
// execvpe is glibc-specific; not present on macOS.
#endif
