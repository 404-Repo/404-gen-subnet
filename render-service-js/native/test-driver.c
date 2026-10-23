#define _GNU_SOURCE
#include <pthread.h>
#include <sched.h>

int request_default_affinity(void) {
    cpu_set_t inherited, requested;
    int result = pthread_getaffinity_np(pthread_self(), sizeof(inherited), &inherited);
    if (result) return result;
    CPU_ZERO(&requested);
    int count = CPU_COUNT(&inherited);
    for (int cpu = 0; cpu < count; cpu++) CPU_SET(cpu, &requested);
    volatile int applied = pthread_setaffinity_np(pthread_self(), sizeof(requested), &requested);
    return applied;
}

int request_explicit_cpu(int cpu) {
    cpu_set_t requested;
    CPU_ZERO(&requested);
    CPU_SET(cpu, &requested);
    volatile int applied = pthread_setaffinity_np(pthread_self(), sizeof(requested), &requested);
    return applied;
}
