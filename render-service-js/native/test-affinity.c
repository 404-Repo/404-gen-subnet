#define _GNU_SOURCE
#include <assert.h>
#include <dlfcn.h>
#include <pthread.h>
#include <sched.h>
#include <stdio.h>
#include <string.h>

int main(int argc, char **argv) {
    assert(argc == 3);
    int fixed = strcmp(argv[2], "fixed") == 0;
    void *driver = dlopen(argv[1], RTLD_NOW);
    assert(driver);
    int (*request_default)(void) = (int (*)(void))dlsym(driver, "request_default_affinity");
    int (*request_explicit)(int) = (int (*)(int))dlsym(driver, "request_explicit_cpu");
    assert(request_default && request_explicit);
    cpu_set_t original, subset, actual, prefix;
    assert(pthread_getaffinity_np(pthread_self(), sizeof(original), &original) == 0);
    assert(CPU_COUNT(&original) >= 2);
    int first = -1, last = -1;
    for (int cpu = 0; cpu < CPU_SETSIZE; cpu++) {
        if (CPU_ISSET(cpu, &original)) {
            if (first < 0) first = cpu;
            last = cpu;
        }
    }
    assert(last > 1);
    CPU_ZERO(&subset);
    CPU_SET(first, &subset);
    CPU_SET(last, &subset);
    assert(pthread_setaffinity_np(pthread_self(), sizeof(subset), &subset) == 0);
    int result = request_default();
    assert(pthread_getaffinity_np(pthread_self(), sizeof(actual), &actual) == 0);
    if (fixed) assert(result == 0 && CPU_EQUAL(&actual, &subset));
    else assert(result != 0 || !CPU_EQUAL(&actual, &subset));

    assert(pthread_setaffinity_np(pthread_self(), sizeof(subset), &subset) == 0);
    assert(request_explicit(last) == 0);
    assert(pthread_getaffinity_np(pthread_self(), sizeof(actual), &actual) == 0);
    assert(CPU_COUNT(&actual) == 1 && CPU_ISSET(last, &actual));

    assert(pthread_setaffinity_np(pthread_self(), sizeof(subset), &subset) == 0);
    CPU_ZERO(&prefix);
    CPU_SET(0, &prefix);
    CPU_SET(1, &prefix);
    result = pthread_setaffinity_np(pthread_self(), sizeof(prefix), &prefix);
    assert(pthread_getaffinity_np(pthread_self(), sizeof(actual), &actual) == 0);
    assert(result != 0 || !CPU_EQUAL(&actual, &subset));
    assert(pthread_setaffinity_np(pthread_self(), sizeof(original), &original) == 0);
    printf("{\"fixed\":%s,\"noncontiguous_cpu_ids\":[%d,%d],\"default_mask_case_passed\":true,\"explicit_driver_mask_preserved\":true,\"other_caller_preserved\":true}\n", fixed ? "true" : "false", first, last);
    dlclose(driver);
    return 0;
}
