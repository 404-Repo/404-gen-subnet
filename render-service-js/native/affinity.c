#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <string.h>

typedef int (*SetAffinity)(pthread_t, size_t, const cpu_set_t *);
static SetAffinity next_set_affinity;
static pthread_once_t resolve_once = PTHREAD_ONCE_INIT;

static void resolve_set_affinity(void) {
    next_set_affinity = (SetAffinity)dlsym(RTLD_NEXT, "pthread_setaffinity_np");
}

/* Preserve inherited CPU IDs for SwiftShader's default count-based mask. */
__attribute__((visibility("default")))
int pthread_setaffinity_np(pthread_t thread, size_t size, const cpu_set_t *requested) {
    pthread_once(&resolve_once, resolve_set_affinity);
    if (!next_set_affinity) return ENOSYS;

    Dl_info caller;
    void *address = __builtin_extract_return_addr(__builtin_return_address(0));
    if (size == sizeof(cpu_set_t) && pthread_equal(thread, pthread_self()) &&
        dladdr(address, &caller) && caller.dli_fname) {
        const char *name = strrchr(caller.dli_fname, '/');
        name = name ? name + 1 : caller.dli_fname;
        if (strcmp(name, "libvk_swiftshader.so") == 0) {
            cpu_set_t inherited;
            if (pthread_getaffinity_np(thread, sizeof(inherited), &inherited) == 0) {
                int count = CPU_COUNT(&inherited);
                cpu_set_t prefix;
                CPU_ZERO(&prefix);
                for (int cpu = 0; cpu < count; cpu++) CPU_SET(cpu, &prefix);
                if (count > 0 && CPU_EQUAL(requested, &prefix)) {
                    return next_set_affinity(thread, sizeof(inherited), &inherited);
                }
            }
        }
    }
    return next_set_affinity(thread, size, requested);
}
