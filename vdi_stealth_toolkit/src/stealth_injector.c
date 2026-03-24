#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <dlfcn.h>
#include <unistd.h>
#include <fcntl.h>

/**
 * VDI Stealth Injector - High Precision Version
 */

#define KEY 0x42

// Internal XOR decoder
static void __x(char *s, int len) {
    for (int i = 0; i < len; i++) s[i] ^= KEY;
}

/* // Log disabled to avoid leaving traces
static void log_shim(const char *msg) {
    int fd = open("/tmp/vdi_shim.log", O_WRONLY | O_APPEND | O_CREAT, 0666);
    if (fd >= 0) {
        write(fd, msg, strlen(msg));
        write(fd, "\n", 1);
        close(fd);
    }
}
*/

typedef int (*main_t)(int, char **, char **);

int __libc_start_main(main_t main, int argc, char **argv,
                      void (*init) (void), void (*fini) (void),
                      void (*rtld_fini) (void), void *stack_end) {

    typeof(&__libc_start_main) o = (typeof(&__libc_start_main))dlsym(RTLD_NEXT, "__libc_start_main");

    if (argc > 0 && argv && argv[0]) {
        // [Verified] "cmcc-jtydn"
        unsigned char t_hex[] = {0x21, 0x2f, 0x21, 0x21, 0x6f, 0x28, 0x36, 0x3b, 0x26, 0x2c, 0x00};
        __x((char*)t_hex, 10);

        if (strstr(argv[0], (char*)t_hex)) {
            // [Verified] "--type="
            unsigned char c_hex[] = {0x6f, 0x6f, 0x36, 0x3b, 0x32, 0x27, 0x7f, 0x00};
            __x((char*)c_hex, 7);

            int is_child = 0;
            for (int i = 1; i < argc; i++) {
                if (argv[i] && strstr(argv[i], (char*)c_hex)) {
                    is_child = 1;
                    break;
                }
            }

            if (!is_child) {
                // log_shim(">>> Main process. Injecting corrected segments...");

                // Flag 1: "--remote-debugging-port=9222"
                unsigned char f1[] = {0x6f, 0x6f, 0x30, 0x27, 0x2f, 0x2d, 0x36, 0x27, 0x6f, 0x26, 0x27, 0x20, 0x37, 0x25, 0x25, 0x2b, 0x2c, 0x25, 0x6f, 0x32, 0x2d, 0x30, 0x36, 0x7f, 0x7b, 0x70, 0x70, 0x70, 0x00};
                // Flag 2: "--no-sandbox"
                unsigned char f2[] = {0x6f, 0x6f, 0x2c, 0x2d, 0x6f, 0x31, 0x23, 0x2c, 0x26, 0x20, 0x2d, 0x3a, 0x00};
                // Flag 3: "--remote-allow-origins=*"
                unsigned char f3[] = {0x6f, 0x6f, 0x30, 0x27, 0x2f, 0x2d, 0x36, 0x27, 0x6f, 0x23, 0x2e, 0x2e, 0x2d, 0x35, 0x6f, 0x2d, 0x30, 0x2b, 0x25, 0x2b, 0x2c, 0x31, 0x7f, 0x68, 0x00};
                
                __x((char*)f1, 28);
                __x((char*)f2, 12);
                __x((char*)f3, 24);

                int na = argc + 3;
                char **nv = malloc((na + 1) * sizeof(char *));
                if (nv) {
                    nv[0] = argv[0];
                    nv[1] = strdup((char*)f1);
                    nv[2] = strdup((char*)f2);
                    nv[3] = strdup((char*)f3);
                    
                    int cur = 4;
                    for (int i = 1; i < argc; i++) {
                        if (strcmp(argv[i], "%U") == 0) continue;
                        nv[cur++] = argv[i];
                    }
                    nv[cur] = NULL;
                    
                    // log_shim("Correction complete. Handing over to libc.");
                    return o(main, cur, nv, init, fini, rtld_fini, stack_end);
                }
            }
        }
    }

    return o(main, argc, argv, init, fini, rtld_fini, stack_end);
}
