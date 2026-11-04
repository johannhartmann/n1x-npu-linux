#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include "n1x_compiler.h"

#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include <map>
#include <mutex>
#include <set>
#include <string>
#include <vector>

namespace {

constexpr uint64_t kWinPreferredBase = 0x180000000ULL;
constexpr size_t kWinImageSize = 0x1aa8000ULL;

constexpr uint64_t kRvaTlsStart = 0x128a740ULL;
constexpr uint64_t kRvaTlsEnd = 0x128a8f0ULL;
constexpr uint64_t kRvaTlsIndex = 0x1a18b10ULL;
constexpr uint64_t kRvaDataStart = 0x15a0000ULL;
constexpr size_t kRvaDataSize = 0x47c904ULL;

constexpr uint64_t kRvaLseCheck = 0x1ddcULL;
constexpr uint64_t kRvaAtexit = 0xf02e08ULL;
constexpr uint64_t kRvaGetenv = 0xf0c6b8ULL;

constexpr uint64_t kRvaCrtInit0 = 0xf267d0ULL;
constexpr uint64_t kRvaCrtInit1 = 0xf190e0ULL;
constexpr uint64_t kRvaCrtInit2 = 0xf1dce0ULL;
constexpr uint64_t kRvaCrtInit3 = 0xf2fa98ULL;
constexpr uint64_t kRvaCrtInit4 = 0xf1f750ULL;
constexpr uint64_t kRvaCrtInit5 = 0xf07dc0ULL;
constexpr uint64_t kRvaInitterm = 0xf19358ULL;
constexpr uint64_t kRvaInitArrayBegin = 0x10523b0ULL;
constexpr uint64_t kRvaInitArrayEnd = 0x1055af0ULL;

constexpr uint64_t kRvaCreateMtl3 = 0x626590ULL;
constexpr uint64_t kRvaMtl3ScalarDtor = 0x61c070ULL;
constexpr uint64_t kRvaMtl3GetVersion = 0x6244b0ULL;
constexpr uint64_t kRvaMtl3PreTranslate = 0x624630ULL;
constexpr uint64_t kRvaMtl3Translate = 0x624eb0ULL;
constexpr uint64_t kRvaMtl3PostTranslate = 0x6244d0ULL;
constexpr uint64_t kRvaMtl3GetMetrics = 0x624480ULL;
constexpr uint64_t kRvaMtl3GetBlobSize = 0x624420ULL;
constexpr uint64_t kRvaMtl3CopyBlob = 0x6243a0ULL;

constexpr uint64_t kLinuxOffsetNvDlaCreateNetwork = 0x632c70ULL;
constexpr uint64_t kLinuxOffsetMtlCreate = 0x648150ULL;
constexpr uint64_t kLinuxOffsetMtlCreateLegacy = 0x649be0ULL;
constexpr uint64_t kLinuxStaticIdCounters[5] = {
    0xc9e62cULL, /* nvdla::priv::canonical_ast::Edge::s_next_id */
    0xc9e630ULL, /* nvdla::priv::canonical_ast::Node::s_next_id */
    0xca0a58ULL, /* nvdla::priv::engine_ast::Graph::s_next_id */
    0xca0a5cULL, /* nvdla::priv::engine_ast::Edge::s_next_id */
    0xca0a60ULL, /* nvdla::priv::engine_ast::Node::s_next_id */
};

std::mutex g_init_mutex;
bool g_initialized = false;
void *g_linux_dl = nullptr;
uint8_t *g_linux_base = nullptr;
uint8_t *g_win_base = nullptr;
uint8_t *g_win_teb = nullptr;
uint8_t *g_win_tls_block = nullptr;

uint32_t g_win_last_error = 0;
uint64_t g_win_ticks = 1000;
constexpr uint64_t kWinProcessHeap = 0x12340000ULL;

std::map<uint32_t, uint64_t> g_fls_values;
uint32_t g_next_fls_index = 1;
std::map<std::string, uint64_t> g_loaded_modules;
std::set<std::string> g_virtual_dirs;

uint8_t *g_win_heap_base = nullptr;
constexpr size_t kWinHeapSize = 2048ULL * 1024 * 1024;
size_t g_win_heap_cursor = 0;
size_t g_win_heap_watermark = 0;
std::vector<uint8_t> g_win_data_snapshot;
std::vector<uint8_t> g_win_tls_snapshot;
std::vector<uint8_t> g_win_heap_snapshot;

struct AllocHeader {
    uint64_t magic;
    size_t size;
    size_t capacity;
};
constexpr uint64_t kAllocMagic = 0x4e31584845415030ULL;
std::map<size_t, std::vector<void *>> g_win_free_lists;

inline void restore_win_x18() {
    asm volatile("mov x18, %0" :: "r"(g_win_teb) : "memory");
}

__attribute__((noinline)) uint64_t win_call0(void *fn) {
    uint64_t ret;
    asm volatile(
        "str x18, [sp, #-16]!\n\t"
        "mov x18, %1\n\t"
        "mov x16, %2\n\t"
        "blr x16\n\t"
        "mov %0, x0\n\t"
        "ldr x18, [sp], #16\n\t"
        : "=r"(ret)
        : "r"(g_win_teb), "r"(fn)
        : "x0", "x1", "x2", "x3", "x4", "x5", "x6", "x7",
          "x8", "x9", "x10", "x11", "x12", "x13", "x14", "x15", "x16", "x17",
          "v0", "v1", "v2", "v3", "v4", "v5", "v6", "v7",
          "v16", "v17", "v18", "v19", "v20", "v21", "v22", "v23",
          "v24", "v25", "v26", "v27", "v28", "v29", "v30", "v31",
          "cc", "memory");
    return ret;
}

__attribute__((noinline)) uint64_t win_call1(void *fn, uint64_t a0) {
    uint64_t ret;
    asm volatile(
        "str x18, [sp, #-16]!\n\t"
        "mov x18, %1\n\t"
        "mov x0, %3\n\t"
        "mov x16, %2\n\t"
        "blr x16\n\t"
        "mov %0, x0\n\t"
        "ldr x18, [sp], #16\n\t"
        : "=r"(ret)
        : "r"(g_win_teb), "r"(fn), "r"(a0)
        : "x0", "x1", "x2", "x3", "x4", "x5", "x6", "x7",
          "x8", "x9", "x10", "x11", "x12", "x13", "x14", "x15", "x16", "x17",
          "v0", "v1", "v2", "v3", "v4", "v5", "v6", "v7",
          "v16", "v17", "v18", "v19", "v20", "v21", "v22", "v23",
          "v24", "v25", "v26", "v27", "v28", "v29", "v30", "v31",
          "cc", "memory");
    return ret;
}

__attribute__((noinline)) uint64_t win_call2(void *fn, uint64_t a0, uint64_t a1) {
    uint64_t ret;
    asm volatile(
        "str x18, [sp, #-16]!\n\t"
        "mov x18, %1\n\t"
        "mov x0, %3\n\t"
        "mov x1, %4\n\t"
        "mov x16, %2\n\t"
        "blr x16\n\t"
        "mov %0, x0\n\t"
        "ldr x18, [sp], #16\n\t"
        : "=r"(ret)
        : "r"(g_win_teb), "r"(fn), "r"(a0), "r"(a1)
        : "x0", "x1", "x2", "x3", "x4", "x5", "x6", "x7",
          "x8", "x9", "x10", "x11", "x12", "x13", "x14", "x15", "x16", "x17",
          "v0", "v1", "v2", "v3", "v4", "v5", "v6", "v7",
          "v16", "v17", "v18", "v19", "v20", "v21", "v22", "v23",
          "v24", "v25", "v26", "v27", "v28", "v29", "v30", "v31",
          "cc", "memory");
    return ret;
}

__attribute__((noinline)) uint64_t win_call3(void *fn, uint64_t a0, uint64_t a1, uint64_t a2) {
    uint64_t ret;
    asm volatile(
        "str x18, [sp, #-16]!\n\t"
        "mov x18, %1\n\t"
        "mov x0, %3\n\t"
        "mov x1, %4\n\t"
        "mov x2, %5\n\t"
        "mov x16, %2\n\t"
        "blr x16\n\t"
        "mov %0, x0\n\t"
        "ldr x18, [sp], #16\n\t"
        : "=r"(ret)
        : "r"(g_win_teb), "r"(fn), "r"(a0), "r"(a1), "r"(a2)
        : "x0", "x1", "x2", "x3", "x4", "x5", "x6", "x7",
          "x8", "x9", "x10", "x11", "x12", "x13", "x14", "x15", "x16", "x17",
          "v0", "v1", "v2", "v3", "v4", "v5", "v6", "v7",
          "v16", "v17", "v18", "v19", "v20", "v21", "v22", "v23",
          "v24", "v25", "v26", "v27", "v28", "v29", "v30", "v31",
          "cc", "memory");
    return ret;
}

std::string wide_to_ascii(const uint16_t *wstr, int max_chars = 512) {
    if (!wstr) return "";
    std::string out;
    for (int i = 0; i < max_chars && wstr[i] != 0; ++i) {
        out.push_back(static_cast<char>(wstr[i] & 0xff));
    }
    return out;
}

/* Windows IAT Shims */
extern "C" {

void win_noop_lock(void *) {
    restore_win_x18();
}

uint32_t win_TryAcquireSRWLockExclusive(void *) {
    restore_win_x18();
    return 1;
}

uint32_t win_InitializeCriticalSectionEx(void *, uint32_t, uint32_t) {
    restore_win_x18();
    return 1;
}

void win_DeleteCriticalSection(void *) {
    restore_win_x18();
}

uint32_t win_GetCurrentThreadId() {
    restore_win_x18();
    return 1;
}

uint32_t win_GetCurrentProcessId() {
    restore_win_x18();
    return static_cast<uint32_t>(getpid());
}

uint64_t win_GetCurrentProcess() {
    restore_win_x18();
    return 0xffffffffffffffffULL;
}

uint64_t win_GetProcessHeap() {
    restore_win_x18();
    return kWinProcessHeap;
}

void *win_HeapAlloc(uint64_t, uint32_t flags, size_t bytes) {
    size_t actual = (bytes == 0) ? 1 : bytes;
    size_t aligned = (actual + 15) & ~size_t(15);
    auto it = g_win_free_lists.find(aligned);
    if (it != g_win_free_lists.end() && !it->second.empty()) {
        void *raw = it->second.back();
        it->second.pop_back();
        auto *hdr = static_cast<AllocHeader *>(raw);
        hdr->magic = kAllocMagic;
        hdr->size = actual;
        hdr->capacity = aligned;
        void *user = static_cast<uint8_t *>(raw) + sizeof(AllocHeader);
        if (flags & 0x8) {
            memset(user, 0, actual);
        }
        restore_win_x18();
        return user;
    }
    size_t total = sizeof(AllocHeader) + aligned;
    if (!g_win_heap_base || g_win_heap_cursor + total > kWinHeapSize) {
        restore_win_x18();
        return nullptr;
    }
    void *raw = g_win_heap_base + g_win_heap_cursor;
    g_win_heap_cursor += total;
    auto *hdr = static_cast<AllocHeader *>(raw);
    hdr->magic = kAllocMagic;
    hdr->size = actual;
    hdr->capacity = aligned;
    void *user = static_cast<uint8_t *>(raw) + sizeof(AllocHeader);
    if (flags & 0x8) {
        memset(user, 0, actual);
    }
    restore_win_x18();
    return user;
}

uint32_t win_HeapFree(uint64_t, uint32_t, void *ptr) {
    if (ptr && g_win_heap_base) {
        auto *raw = static_cast<uint8_t *>(ptr) - sizeof(AllocHeader);
        auto *hdr = reinterpret_cast<AllocHeader *>(raw);
        size_t offset = static_cast<size_t>(raw - g_win_heap_base);
        if (hdr->magic == kAllocMagic && offset >= g_win_heap_watermark && g_win_heap_watermark > 0) {
            size_t total = sizeof(AllocHeader) + hdr->capacity;
            if (offset + total == g_win_heap_cursor) {
                g_win_heap_cursor = offset;
            } else {
                g_win_free_lists[hdr->capacity].push_back(raw);
            }
        }
    }
    restore_win_x18();
    return 1;
}

void *win_HeapReAlloc(uint64_t heap, uint32_t flags, void *ptr, size_t bytes) {
    if (!ptr) {
        return win_HeapAlloc(heap, flags, bytes);
    }
    auto *hdr = reinterpret_cast<AllocHeader *>(static_cast<uint8_t *>(ptr) - sizeof(AllocHeader));
    size_t old_size = (hdr->magic == kAllocMagic) ? hdr->size : 0;
    void *new_ptr = win_HeapAlloc(heap, flags, bytes);
    if (new_ptr && old_size > 0) {
        memcpy(new_ptr, ptr, old_size < bytes ? old_size : bytes);
    }
    win_HeapFree(heap, 0, ptr);
    restore_win_x18();
    return new_ptr;
}

size_t win_HeapSize(uint64_t, uint32_t, const void *ptr) {
    if (!ptr) {
        restore_win_x18();
        return 0;
    }
    auto *hdr = reinterpret_cast<const AllocHeader *>(static_cast<const uint8_t *>(ptr) - sizeof(AllocHeader));
    size_t sz = (hdr->magic == kAllocMagic) ? hdr->size : 0;
    restore_win_x18();
    return sz;
}

void *win_EncodePointer(void *ptr) {
    restore_win_x18();
    return ptr;
}

void *win_DecodePointer(void *ptr) {
    restore_win_x18();
    return ptr;
}

uint32_t win_GetLastError() {
    restore_win_x18();
    return g_win_last_error;
}

void win_SetLastError(uint32_t err) {
    g_win_last_error = err;
    restore_win_x18();
}

uint32_t win_FlsAlloc(void *) {
    uint32_t idx = g_next_fls_index++;
    g_fls_values[idx] = 0;
    restore_win_x18();
    return idx;
}

uint64_t win_FlsGetValue(uint32_t idx) {
    auto it = g_fls_values.find(idx);
    g_win_last_error = 0;
    uint64_t val = (it != g_fls_values.end()) ? it->second : 0;
    restore_win_x18();
    return val;
}

uint32_t win_FlsSetValue(uint32_t idx, uint64_t val) {
    g_fls_values[idx] = val;
    restore_win_x18();
    return 1;
}

uint32_t win_FlsFree(uint32_t idx) {
    g_fls_values.erase(idx);
    restore_win_x18();
    return 1;
}

uint32_t win_AreFileApisANSI() {
    restore_win_x18();
    return 1;
}

uint64_t win_LoadLibraryExW(const uint16_t *name, uint64_t, uint32_t) {
    std::string mod = wide_to_ascii(name);
    auto it = g_loaded_modules.find(mod);
    if (it == g_loaded_modules.end()) {
        uint64_t handle = 0x1234ULL + g_loaded_modules.size();
        g_loaded_modules[mod] = handle;
        restore_win_x18();
        return handle;
    }
    restore_win_x18();
    return it->second;
}

void *win_GetProcAddress(uint64_t, const char *func_name) {
    if (func_name && strcmp(func_name, "AreFileApisANSI") == 0) {
        restore_win_x18();
        return reinterpret_cast<void *>(&win_AreFileApisANSI);
    }
    g_win_last_error = 127; /* ERROR_PROC_NOT_FOUND */
    restore_win_x18();
    return nullptr;
}

uint32_t win_FreeLibrary(uint64_t) {
    restore_win_x18();
    return 1;
}

uint32_t win_VirtualProtect(void *, size_t, uint32_t, uint32_t *old_prot) {
    if (old_prot) {
        *old_prot = 4; /* PAGE_READWRITE */
    }
    restore_win_x18();
    return 1;
}

void win_GetStartupInfoW(void *info) {
    if (info) {
        memset(info, 0, 104);
        * static_cast<uint32_t *>(info) = 104;
    }
    restore_win_x18();
}

uint64_t win_GetStdHandle(uint32_t) {
    restore_win_x18();
    return 0;
}

uint64_t win_CreateFileW(const uint16_t *, uint32_t, uint32_t, void *, uint32_t, uint32_t, uint64_t) {
    g_win_last_error = 2; /* ERROR_FILE_NOT_FOUND */
    restore_win_x18();
    return 0xffffffffffffffffULL; /* INVALID_HANDLE_VALUE */
}

uint32_t win_CreateDirectoryW(const uint16_t *path, void *) {
    std::string dir = wide_to_ascii(path);
    if (g_virtual_dirs.count(dir)) {
        g_win_last_error = 183; /* ERROR_ALREADY_EXISTS */
        restore_win_x18();
        return 0;
    }
    g_virtual_dirs.insert(dir);
    restore_win_x18();
    return 1;
}

uint32_t win_GetFileAttributesExW(const uint16_t *path, uint32_t, void *info) {
    std::string filename = wide_to_ascii(path);
    if (g_virtual_dirs.count(filename)) {
        if (info) {
            memset(info, 0, 36);
            *static_cast<uint32_t *>(info) = 16; /* FILE_ATTRIBUTE_DIRECTORY */
        }
        restore_win_x18();
        return 1;
    }
    g_win_last_error = 2; /* ERROR_FILE_NOT_FOUND */
    restore_win_x18();
    return 0;
}

int32_t win_MultiByteToWideChar(uint32_t, uint32_t, const char *src, int32_t src_len, uint16_t *dst, int32_t dst_cap) {
    if (!src) {
        restore_win_x18();
        return 0;
    }
    int32_t count = (src_len == -1) ? static_cast<int32_t>(strlen(src) + 1) : src_len;
    if (dst_cap > 0 && dst) {
        int32_t to_copy = count < dst_cap ? count : dst_cap;
        for (int32_t i = 0; i < to_copy; ++i) {
            dst[i] = static_cast<uint8_t>(src[i]);
        }
    }
    restore_win_x18();
    return count;
}

int32_t win_WideCharToMultiByte(uint32_t, uint32_t, const uint16_t *src, int32_t src_len,
                                char *dst, int32_t dst_cap, const char *, uint32_t *used_default) {
    if (used_default) {
        *used_default = 0;
    }
    if (!src) {
        restore_win_x18();
        return 0;
    }
    int32_t count = 0;
    if (src_len == -1) {
        while (src[count] != 0) ++count;
        ++count;
    } else {
        count = src_len;
    }
    if (dst_cap > 0 && dst) {
        int32_t to_copy = count < dst_cap ? count : dst_cap;
        for (int32_t i = 0; i < to_copy; ++i) {
            dst[i] = static_cast<char>(src[i] & 0xff);
        }
    }
    restore_win_x18();
    return count;
}

uint32_t win_QueryPerformanceFrequency(uint64_t *freq) {
    if (freq) {
        *freq = 1000000ULL;
    }
    restore_win_x18();
    return 1;
}

uint32_t win_QueryPerformanceCounter(uint64_t *counter) {
    g_win_ticks += 1000ULL;
    if (counter) {
        *counter = g_win_ticks;
    }
    restore_win_x18();
    return 1;
}

void win_GetSystemTimeAsFileTime(uint64_t *ft) {
    if (ft) {
        *ft = 133000000000000000ULL;
    }
    restore_win_x18();
}

uint32_t win_CloseHandle(uint64_t) {
    restore_win_x18();
    return 1;
}

/* Itanium C++ vtable wrappers for Windows Mtl3 */
void shim_mtl3_d1(void *obj) {
    win_call2(g_win_base + kRvaMtl3ScalarDtor, reinterpret_cast<uint64_t>(obj), 0);
}

void shim_mtl3_d0(void *obj) {
    win_call2(g_win_base + kRvaMtl3ScalarDtor, reinterpret_cast<uint64_t>(obj), 1);
}

uint32_t shim_mtl3_get_version(void *obj, void *ver_out) {
    return static_cast<uint32_t>(
        win_call2(g_win_base + kRvaMtl3GetVersion,
                  reinterpret_cast<uint64_t>(obj),
                  reinterpret_cast<uint64_t>(ver_out)));
}

uint32_t shim_mtl3_set_device_misc_config(void *, void *, void *, void *) {
    /* Do not store stack-allocated config pointers from nvdla_mtl_set_config into Mtl3. */
    return 0;
}

uint32_t shim_mtl3_query_device_info(void *, void *) {
    return 0;
}

uint32_t shim_mtl3_pre_translate(void *obj, void *desc_list) {
    return static_cast<uint32_t>(
        win_call2(g_win_base + kRvaMtl3PreTranslate,
                  reinterpret_cast<uint64_t>(obj),
                  reinterpret_cast<uint64_t>(desc_list)));
}

uint32_t shim_mtl3_translate(void *obj, void *desc_list) {
    return static_cast<uint32_t>(
        win_call2(g_win_base + kRvaMtl3Translate,
                  reinterpret_cast<uint64_t>(obj),
                  reinterpret_cast<uint64_t>(desc_list)));
}

uint32_t shim_mtl3_post_translate(void *obj) {
    return static_cast<uint32_t>(
        win_call1(g_win_base + kRvaMtl3PostTranslate,
                  reinterpret_cast<uint64_t>(obj)));
}

uint32_t shim_mtl3_get_metrics(void *obj, void *metrics_out) {
    return static_cast<uint32_t>(
        win_call2(g_win_base + kRvaMtl3GetMetrics,
                  reinterpret_cast<uint64_t>(obj),
                  reinterpret_cast<uint64_t>(metrics_out)));
}

uint32_t shim_mtl3_get_blob_buffer_size(void *obj, const void *blob_type, void *size_out) {
    return static_cast<uint32_t>(
        win_call3(g_win_base + kRvaMtl3GetBlobSize,
                  reinterpret_cast<uint64_t>(obj),
                  reinterpret_cast<uint64_t>(blob_type),
                  reinterpret_cast<uint64_t>(size_out)));
}

uint32_t shim_mtl3_copy_blob_buffer(void *obj, const void *blob_type, void *dst_buf) {
    return static_cast<uint32_t>(
        win_call3(g_win_base + kRvaMtl3CopyBlob,
                  reinterpret_cast<uint64_t>(obj),
                  reinterpret_cast<uint64_t>(blob_type),
                  reinterpret_cast<uint64_t>(dst_buf)));
}

uint32_t shim_mtl3_set_log_path(void *, const void *) {
    return 0;
}

uint32_t shim_mtl3_enable_switch_point(void *) {
    return 0;
}

uint32_t shim_mtl3_disable_switch_point(void *) {
    return 0;
}

void *g_linux_mtl3_vtable[14] = {
    reinterpret_cast<void *>(&shim_mtl3_d1),
    reinterpret_cast<void *>(&shim_mtl3_d0),
    reinterpret_cast<void *>(&shim_mtl3_get_version),
    reinterpret_cast<void *>(&shim_mtl3_set_device_misc_config),
    reinterpret_cast<void *>(&shim_mtl3_query_device_info),
    reinterpret_cast<void *>(&shim_mtl3_pre_translate),
    reinterpret_cast<void *>(&shim_mtl3_translate),
    reinterpret_cast<void *>(&shim_mtl3_post_translate),
    reinterpret_cast<void *>(&shim_mtl3_get_metrics),
    reinterpret_cast<void *>(&shim_mtl3_get_blob_buffer_size),
    reinterpret_cast<void *>(&shim_mtl3_copy_blob_buffer),
    reinterpret_cast<void *>(&shim_mtl3_set_log_path),
    reinterpret_cast<void *>(&shim_mtl3_enable_switch_point),
    reinterpret_cast<void *>(&shim_mtl3_disable_switch_point),
};

void *hook_nvdla_mtl_create(uint32_t a, uint32_t b, uint32_t chip, uint32_t backend) {
    auto *ctx = static_cast<uint8_t *>(calloc(1, 24));
    if (!ctx) {
        return nullptr;
    }
    *reinterpret_cast<uint32_t *>(ctx + 0) = chip;
    *reinterpret_cast<uint32_t *>(ctx + 4) = backend;
    *reinterpret_cast<uint32_t *>(ctx + 8) = a & 0xffffU;
    *reinterpret_cast<uint32_t *>(ctx + 12) = b & 0xffU;

    if (backend == 3) {
        uint64_t mtl3 = win_call0(g_win_base + kRvaCreateMtl3);
        if (!mtl3) {
            free(ctx);
            return nullptr;
        }
        *reinterpret_cast<void **>(mtl3) = g_linux_mtl3_vtable;
        *reinterpret_cast<uint64_t *>(ctx + 16) = mtl3;
        return ctx;
    }
    if (backend == 1 || backend == 2) {
        using LegacyCreateFn = void *(*)(uint32_t, uint32_t);
        auto fn = reinterpret_cast<LegacyCreateFn>(g_linux_base + kLinuxOffsetMtlCreateLegacy);
        void *impl = fn(a & 0xffffU, b & 0xffU);
        if (!impl) {
            free(ctx);
            return nullptr;
        }
        *reinterpret_cast<void **>(ctx + 16) = impl;
        return ctx;
    }
    free(ctx);
    return nullptr;
}

} /* extern "C" */

struct PeSectionHeader {
    char name[8];
    uint32_t virtual_size;
    uint32_t virtual_address;
    uint32_t size_of_raw_data;
    uint32_t pointer_to_raw_data;
    uint32_t pointer_to_relocations;
    uint32_t pointer_to_linenumbers;
    uint16_t number_of_relocations;
    uint16_t number_of_linenumbers;
    uint32_t characteristics;
};

struct PeImportDescriptor {
    uint32_t original_first_thunk;
    uint32_t time_date_stamp;
    uint32_t forwarder_chain;
    uint32_t name;
    uint32_t first_thunk;
};

int map_and_init_windows_dll(const char *dll_path) {
    int fd = open(dll_path, O_RDONLY);
    if (fd < 0) {
        return -1;
    }
    struct stat st {};
    if (fstat(fd, &st) != 0 || st.st_size < 0x1000) {
        close(fd);
        return -1;
    }
    std::vector<uint8_t> raw(static_cast<size_t>(st.st_size));
    size_t read_total = 0;
    while (read_total < raw.size()) {
        ssize_t n = read(fd, raw.data() + read_total, raw.size() - read_total);
        if (n <= 0) {
            close(fd);
            return -1;
        }
        read_total += static_cast<size_t>(n);
    }
    close(fd);

    uint32_t pe_offset = *reinterpret_cast<const uint32_t *>(raw.data() + 0x3c);
    if (pe_offset + 0x108 > raw.size() || memcmp(raw.data() + pe_offset, "PE\0\0", 4) != 0) {
        return -1;
    }
    uint16_t num_sections = *reinterpret_cast<const uint16_t *>(raw.data() + pe_offset + 6);
    uint16_t opt_hdr_size = *reinterpret_cast<const uint16_t *>(raw.data() + pe_offset + 20);
    const uint8_t *opt_hdr = raw.data() + pe_offset + 24;
    uint32_t size_of_headers = *reinterpret_cast<const uint32_t *>(opt_hdr + 60);

    void *mapped = mmap(reinterpret_cast<void *>(kWinPreferredBase),
                        kWinImageSize,
                        PROT_READ | PROT_WRITE | PROT_EXEC,
                        MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED_NOREPLACE,
                        -1,
                        0);
    if (mapped == MAP_FAILED) {
        return -1;
    }
    g_win_base = static_cast<uint8_t *>(mapped);
    memcpy(g_win_base, raw.data(), size_of_headers);

    const auto *sections = reinterpret_cast<const PeSectionHeader *>(opt_hdr + opt_hdr_size);
    for (uint16_t i = 0; i < num_sections; ++i) {
        const auto &sec = sections[i];
        if (sec.size_of_raw_data > 0 && sec.pointer_to_raw_data + sec.size_of_raw_data <= raw.size()) {
            uint32_t copy_len = sec.size_of_raw_data;
            if (sec.virtual_size > 0 && copy_len > sec.virtual_size && sec.virtual_address + copy_len > kWinImageSize) {
                copy_len = sec.virtual_size;
            }
            memcpy(g_win_base + sec.virtual_address, raw.data() + sec.pointer_to_raw_data, copy_len);
        }
    }

    /* Set up deterministic 2 GB Windows heap arena */
    g_win_heap_base = static_cast<uint8_t *>(
        mmap(nullptr, kWinHeapSize, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0));
    if (g_win_heap_base == MAP_FAILED) {
        g_win_heap_base = nullptr;
        return -1;
    }
    g_win_heap_cursor = 0;

    /* Set up TEB and TLS block */
    g_win_teb = static_cast<uint8_t *>(
        mmap(nullptr, 4096, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0));
    if (g_win_teb == MAP_FAILED) {
        return -1;
    }
    size_t tls_size = static_cast<size_t>(kRvaTlsEnd - kRvaTlsStart);
    g_win_tls_block = static_cast<uint8_t *>(calloc(1, tls_size + 256));
    memcpy(g_win_tls_block, g_win_base + kRvaTlsStart, tls_size);

    /* StackLimit = 0 so __chkstk at 0x1070 returns immediately */
    *reinterpret_cast<uint64_t *>(g_win_teb + 0x10) = 0;
    /* ThreadLocalStoragePointer at TEB + 0x58 points to array at TEB + 0x100 */
    *reinterpret_cast<uint64_t *>(g_win_teb + 0x58) = reinterpret_cast<uint64_t>(g_win_teb + 0x100);
    *reinterpret_cast<uint64_t *>(g_win_teb + 0x100) = reinterpret_cast<uint64_t>(g_win_tls_block);
    *reinterpret_cast<uint32_t *>(g_win_base + kRvaTlsIndex) = 0;

    /* Bind IAT imports */
    std::map<std::string, void *> iat_map = {
        {"ReleaseSRWLockExclusive", reinterpret_cast<void *>(&win_noop_lock)},
        {"AcquireSRWLockExclusive", reinterpret_cast<void *>(&win_noop_lock)},
        {"TryAcquireSRWLockExclusive", reinterpret_cast<void *>(&win_TryAcquireSRWLockExclusive)},
        {"InitializeSRWLock", reinterpret_cast<void *>(&win_noop_lock)},
        {"InitializeConditionVariable", reinterpret_cast<void *>(&win_noop_lock)},
        {"WakeConditionVariable", reinterpret_cast<void *>(&win_noop_lock)},
        {"WakeAllConditionVariable", reinterpret_cast<void *>(&win_noop_lock)},
        {"EnterCriticalSection", reinterpret_cast<void *>(&win_noop_lock)},
        {"LeaveCriticalSection", reinterpret_cast<void *>(&win_noop_lock)},
        {"InitializeCriticalSectionEx", reinterpret_cast<void *>(&win_InitializeCriticalSectionEx)},
        {"DeleteCriticalSection", reinterpret_cast<void *>(&win_DeleteCriticalSection)},
        {"InitializeSListHead", reinterpret_cast<void *>(&win_noop_lock)},
        {"GetCurrentThreadId", reinterpret_cast<void *>(&win_GetCurrentThreadId)},
        {"GetCurrentProcessId", reinterpret_cast<void *>(&win_GetCurrentProcessId)},
        {"GetCurrentProcess", reinterpret_cast<void *>(&win_GetCurrentProcess)},
        {"GetProcessHeap", reinterpret_cast<void *>(&win_GetProcessHeap)},
        {"HeapAlloc", reinterpret_cast<void *>(&win_HeapAlloc)},
        {"HeapFree", reinterpret_cast<void *>(&win_HeapFree)},
        {"HeapReAlloc", reinterpret_cast<void *>(&win_HeapReAlloc)},
        {"HeapSize", reinterpret_cast<void *>(&win_HeapSize)},
        {"EncodePointer", reinterpret_cast<void *>(&win_EncodePointer)},
        {"DecodePointer", reinterpret_cast<void *>(&win_DecodePointer)},
        {"GetLastError", reinterpret_cast<void *>(&win_GetLastError)},
        {"SetLastError", reinterpret_cast<void *>(&win_SetLastError)},
        {"FlsAlloc", reinterpret_cast<void *>(&win_FlsAlloc)},
        {"FlsGetValue", reinterpret_cast<void *>(&win_FlsGetValue)},
        {"FlsSetValue", reinterpret_cast<void *>(&win_FlsSetValue)},
        {"FlsFree", reinterpret_cast<void *>(&win_FlsFree)},
        {"LoadLibraryExW", reinterpret_cast<void *>(&win_LoadLibraryExW)},
        {"GetProcAddress", reinterpret_cast<void *>(&win_GetProcAddress)},
        {"FreeLibrary", reinterpret_cast<void *>(&win_FreeLibrary)},
        {"VirtualProtect", reinterpret_cast<void *>(&win_VirtualProtect)},
        {"GetStartupInfoW", reinterpret_cast<void *>(&win_GetStartupInfoW)},
        {"GetStdHandle", reinterpret_cast<void *>(&win_GetStdHandle)},
        {"CreateFileW", reinterpret_cast<void *>(&win_CreateFileW)},
        {"CreateDirectoryW", reinterpret_cast<void *>(&win_CreateDirectoryW)},
        {"GetFileAttributesExW", reinterpret_cast<void *>(&win_GetFileAttributesExW)},
        {"MultiByteToWideChar", reinterpret_cast<void *>(&win_MultiByteToWideChar)},
        {"WideCharToMultiByte", reinterpret_cast<void *>(&win_WideCharToMultiByte)},
        {"QueryPerformanceFrequency", reinterpret_cast<void *>(&win_QueryPerformanceFrequency)},
        {"QueryPerformanceCounter", reinterpret_cast<void *>(&win_QueryPerformanceCounter)},
        {"GetSystemTimeAsFileTime", reinterpret_cast<void *>(&win_GetSystemTimeAsFileTime)},
        {"GetSystemTimePreciseAsFileTime", reinterpret_cast<void *>(&win_GetSystemTimeAsFileTime)},
        {"AreFileApisANSI", reinterpret_cast<void *>(&win_AreFileApisANSI)},
        {"CloseHandle", reinterpret_cast<void *>(&win_CloseHandle)},
    };

    uint32_t import_dir_rva = *reinterpret_cast<const uint32_t *>(opt_hdr + 112 + 8);
    const auto *imp = reinterpret_cast<const PeImportDescriptor *>(g_win_base + import_dir_rva);
    for (; imp->name != 0; ++imp) {
        uint32_t int_rva = imp->original_first_thunk ? imp->original_first_thunk : imp->first_thunk;
        uint32_t iat_rva = imp->first_thunk;
        const auto *thunk_in = reinterpret_cast<const uint64_t *>(g_win_base + int_rva);
        auto *thunk_out = reinterpret_cast<uint64_t *>(g_win_base + iat_rva);
        for (; *thunk_in != 0; ++thunk_in, ++thunk_out) {
            if ((*thunk_in & (1ULL << 63)) == 0) {
                const char *fn_name = reinterpret_cast<const char *>(g_win_base + (*thunk_in & 0x7fffffffULL) + 2);
                auto it = iat_map.find(fn_name);
                if (it != iat_map.end()) {
                    *thunk_out = reinterpret_cast<uint64_t>(it->second);
                }
            }
        }
    }

    /* Patch EL1 CurrentEL / KUSER_SHARED_DATA LSE check at 0x1ddc:
     * mov x16, #1 (0xd2800030)
     * b #0x1dfc   (0x14000007)
     */
    const uint32_t lse_patch[2] = {0xd2800030U, 0x14000007U};
    memcpy(g_win_base + kRvaLseCheck, lse_patch, sizeof(lse_patch));

    /* Patch atexit (0xf02e08) and getenv (0xf0c6b8) to return 0 immediately:
     * mov x0, #0 (0xd2800000)
     * ret        (0xd65f03c0)
     */
    const uint32_t ret0_patch[2] = {0xd2800000U, 0xd65f03c0U};
    memcpy(g_win_base + kRvaAtexit, ret0_patch, sizeof(ret0_patch));
    memcpy(g_win_base + kRvaGetenv, ret0_patch, sizeof(ret0_patch));
    __builtin___clear_cache(reinterpret_cast<char *>(g_win_base),
                            reinterpret_cast<char *>(g_win_base + kWinImageSize));

    /* Execute the 6 MSVC CRT initialization routines and _initterm (1767 C++ initializers) */
    if (win_call0(g_win_base + kRvaCrtInit0) != 1) return -1;
    if (win_call0(g_win_base + kRvaCrtInit1) != 1) return -1;
    if (win_call0(g_win_base + kRvaCrtInit2) != 1) return -1;
    if (win_call0(g_win_base + kRvaCrtInit3) != 1) return -1;
    if (win_call0(g_win_base + kRvaCrtInit4) != 1) return -1;
    if (win_call0(g_win_base + kRvaCrtInit5) != 0) return -1;

    win_call2(g_win_base + kRvaInitterm,
              reinterpret_cast<uint64_t>(g_win_base + kRvaInitArrayBegin),
              reinterpret_cast<uint64_t>(g_win_base + kRvaInitArrayEnd));

    g_win_heap_watermark = (g_win_heap_cursor + 4095) & ~size_t(4095);
    g_win_heap_cursor = g_win_heap_watermark;
    g_win_data_snapshot.assign(g_win_base + kRvaDataStart, g_win_base + kRvaDataStart + kRvaDataSize);
    g_win_tls_snapshot.assign(g_win_tls_block, g_win_tls_block + tls_size + 256);
    g_win_heap_snapshot.assign(g_win_heap_base, g_win_heap_base + g_win_heap_watermark);
    return 0;
}

int patch_linux_compiler_so(const char *so_path) {
    g_linux_dl = dlopen(so_path, RTLD_NOW | RTLD_LOCAL);
    if (!g_linux_dl) {
        return -1;
    }
    void *sym = dlsym(g_linux_dl, "NvDlaCreateNetwork");
    if (!sym) {
        return -1;
    }
    g_linux_base = static_cast<uint8_t *>(sym) - kLinuxOffsetNvDlaCreateNetwork;

    uint8_t *target = g_linux_base + kLinuxOffsetMtlCreate;
    uintptr_t page_start = reinterpret_cast<uintptr_t>(target) & ~uintptr_t(4095);
    if (mprotect(reinterpret_cast<void *>(page_start), 8192, PROT_READ | PROT_WRITE | PROT_EXEC) != 0) {
        return -1;
    }

    /* Inline 16-byte absolute branch at nvdla_mtl_create (0x648150):
     * ldr x16, #8  (0x58000050)
     * br x16       (0xd61f0200)
     * .quad &hook_nvdla_mtl_create
     */
    struct InlineHook {
        uint32_t ldr_x16;
        uint32_t br_x16;
        uint64_t addr;
    } hook = {
        0x58000050U,
        0xd61f0200U,
        reinterpret_cast<uint64_t>(&hook_nvdla_mtl_create),
    };
    memcpy(target, &hook, sizeof(hook));
    __builtin___clear_cache(reinterpret_cast<char *>(target),
                            reinterpret_cast<char *>(target + sizeof(hook)));
    mprotect(reinterpret_cast<void *>(page_start), 8192, PROT_READ | PROT_EXEC);
    return 0;
}

std::string default_repo_root() {
    Dl_info info {};
    if (dladdr(reinterpret_cast<void *>(&n1x_compiler_init), &info) != 0 && info.dli_fname) {
        std::string p(info.dli_fname);
        auto slash = p.rfind('/');
        if (slash != std::string::npos) {
            return p.substr(0, slash) + "/..";
        }
    }
    return ".";
}

template <typename Fn>
Fn resolve_linux_fn(const char *name) {
    if (!g_initialized) {
        n1x_compiler_init(nullptr, nullptr);
    }
    return reinterpret_cast<Fn>(dlsym(g_linux_dl, name));
}

} /* namespace */

extern "C" {

int n1x_compiler_init(const char *linux_so_path, const char *windows_dll_path) {
    std::lock_guard<std::mutex> lock(g_init_mutex);
    if (g_initialized) {
        return 0;
    }
    std::string root = default_repo_root();
    std::string so = linux_so_path
        ? linux_so_path
        : (root + "/sources/tensorrt-11.4/native/usr/lib/aarch64-linux-gnu/nvidia/libnvdla_compiler.so");
    std::string dll = windows_dll_path
        ? windows_dll_path
        : (root + "/sources/tensorrt-11.4/nvdla_compiler.dll");

    if (map_and_init_windows_dll(dll.c_str()) != 0) {
        return -1;
    }
    if (patch_linux_compiler_so(so.c_str()) != 0) {
        return -2;
    }
    g_initialized = true;
    return 0;
}

void n1x_compiler_free_buffer(void *ptr) {
    free(ptr);
}

#define FORWARD_DLA(ret_type, name, params, args) \
    ret_type name params { \
        static auto fn = resolve_linux_fn<ret_type (*) params>(#name); \
        return fn args; \
    }

uint32_t NvDlaCreateNetwork(N1xDlaHandle *out_network) {
    static auto fn = resolve_linux_fn<uint32_t (*)(N1xDlaHandle *)>("NvDlaCreateNetwork");
    if (g_linux_base) {
        for (uint64_t off : kLinuxStaticIdCounters) {
            *reinterpret_cast<uint32_t *>(g_linux_base + off) = 0;
        }
    }
    if (!g_win_data_snapshot.empty() && g_win_base) {
        memcpy(g_win_base + kRvaDataStart, g_win_data_snapshot.data(), g_win_data_snapshot.size());
    }
    if (!g_win_tls_snapshot.empty() && g_win_tls_block) {
        memcpy(g_win_tls_block, g_win_tls_snapshot.data(), g_win_tls_snapshot.size());
    }
    if (!g_win_heap_snapshot.empty() && g_win_heap_base) {
        memcpy(g_win_heap_base, g_win_heap_snapshot.data(), g_win_heap_snapshot.size());
    }
    if (g_win_heap_cursor > g_win_heap_watermark && g_win_heap_base) {
        memset(g_win_heap_base + g_win_heap_watermark, 0, g_win_heap_cursor - g_win_heap_watermark);
        g_win_heap_cursor = g_win_heap_watermark;
    }
    g_win_free_lists.clear();
    g_win_ticks = 1000;
    g_virtual_dirs.clear();
    return fn(out_network);
}
FORWARD_DLA(void, NvDlaDestroyNetwork, (N1xDlaHandle network), (network))
FORWARD_DLA(uint32_t, NvDlaCreateWisdom, (N1xDlaHandle *out_wisdom), (out_wisdom))
FORWARD_DLA(void, NvDlaDestroyWisdom, (N1xDlaHandle wisdom), (wisdom))

FORWARD_DLA(uint32_t, NvDlaWisdomOpen, (N1xDlaHandle wisdom, const char *path), (wisdom, path))
FORWARD_DLA(uint32_t, NvDlaWisdomClose, (N1xDlaHandle wisdom), (wisdom))
FORWARD_DLA(uint32_t, NvDlaWisdomSetDataType, (N1xDlaHandle wisdom, uint8_t dtype), (wisdom, dtype))
FORWARD_DLA(uint32_t, NvDlaWisdomSetNetworkTransient, (N1xDlaHandle wisdom, N1xDlaHandle network), (wisdom, network))
FORWARD_DLA(uint32_t, NvDlaWisdomGetCompiler, (N1xDlaHandle wisdom, N1xDlaHandle *out_compiler), (wisdom, out_compiler))
FORWARD_DLA(uint32_t, NvDlaWisdomGetProfiler, (N1xDlaHandle wisdom, N1xDlaHandle *out_profiler), (wisdom, out_profiler))

FORWARD_DLA(uint32_t, NvDlaProfilerCreateProfile, (N1xDlaHandle profiler, const char *name, N1xDlaHandle *out_profile), (profiler, name, out_profile))
FORWARD_DLA(uint32_t, NvDlaProfilerGetProfile, (N1xDlaHandle profiler, const char *name, N1xDlaHandle *out_profile), (profiler, name, out_profile))
FORWARD_DLA(uint32_t, NvDlaProfilerGetTargetConfig, (N1xDlaHandle profiler, const char *name, N1xDlaHandle *out_target), (profiler, name, out_target))
FORWARD_DLA(uint32_t, NvDlaTargetConfigGetName, (N1xDlaHandle target, const char **out_name), (target, out_name))

FORWARD_DLA(uint32_t, NvDlaProfileInitWithDefaultProfile, (N1xDlaHandle profile), (profile))
FORWARD_DLA(uint32_t, NvDlaProfileSetComputePrecision, (N1xDlaHandle profile, uint8_t precision), (profile, precision))
FORWARD_DLA(uint32_t, NvDlaProfileSetNetworkInputDataFormat, (N1xDlaHandle profile, uint8_t format), (profile, format))
FORWARD_DLA(uint32_t, NvDlaProfileSetNetworkOutputDataFormat, (N1xDlaHandle profile, uint8_t format), (profile, format))
FORWARD_DLA(uint32_t, NvDlaProfileSetNetworkInputSurfaceFormat, (N1xDlaHandle profile, uint8_t format), (profile, format))
FORWARD_DLA(uint32_t, NvDlaProfileSetNetworkOutputSurfaceFormat, (N1xDlaHandle profile, uint8_t format), (profile, format))
FORWARD_DLA(uint32_t, NvDlaProfileSetTensorScalingMode, (N1xDlaHandle profile, uint8_t mode), (profile, mode))
FORWARD_DLA(uint32_t, NvDlaProfileSetQuantizationMode, (N1xDlaHandle profile, uint8_t mode), (profile, mode))
FORWARD_DLA(uint32_t, NvDlaProfileSetIsExplicitQuantization, (N1xDlaHandle profile, uint8_t is_explicit), (profile, is_explicit))
FORWARD_DLA(uint32_t, NvDlaProfileSetCanGenerateLayerwiseStats, (N1xDlaHandle profile, uint8_t enable), (profile, enable))
FORWARD_DLA(uint32_t, NvDlaProfileSetCanGenerateDetailedLayerwiseStats, (N1xDlaHandle profile, uint8_t enable), (profile, enable))
FORWARD_DLA(uint32_t, NvDlaProfileSetCanWinograd, (N1xDlaHandle profile, uint8_t enable), (profile, enable))
FORWARD_DLA(uint32_t, NvDlaProfileSetCanCompressWeights, (N1xDlaHandle profile, uint8_t enable), (profile, enable))
FORWARD_DLA(uint32_t, NvDlaProfileSetCanCompressStructuredSparseWeights, (N1xDlaHandle profile, uint8_t enable), (profile, enable))
FORWARD_DLA(uint32_t, NvDlaProfileSetCanSDPFuseVerticalOps, (N1xDlaHandle profile, uint8_t enable), (profile, enable))
FORWARD_DLA(uint32_t, NvDlaProfileSetCanSDPFuseSubEngineOps, (N1xDlaHandle profile, uint8_t enable), (profile, enable))
FORWARD_DLA(uint32_t, NvDlaProfileSetCanSDPMergeMathOps, (N1xDlaHandle profile, uint8_t enable), (profile, enable))
FORWARD_DLA(uint32_t, NvDlaProfileSetCanSDPPDPOnFly, (N1xDlaHandle profile, uint8_t enable), (profile, enable))
FORWARD_DLA(uint32_t, NvDlaProfileSetUseSoftMaxOptz, (N1xDlaHandle profile, uint8_t enable), (profile, enable))
FORWARD_DLA(uint32_t, NvDlaProfileSetMultiBatchSize, (N1xDlaHandle profile, uint32_t batch_size), (profile, batch_size))

FORWARD_DLA(uint32_t, NvDlaCompilerCompile, (N1xDlaHandle compiler, const char *profile_name, const char *target_name, N1xDlaHandle *out_loadable), (compiler, profile_name, target_name, out_loadable))
FORWARD_DLA(uint32_t, NvDlaCompilerGetLoadableImageSize, (N1xDlaHandle compiler, const char *profile_name, uint64_t *out_size), (compiler, profile_name, out_size))
FORWARD_DLA(uint32_t, NvDlaCompilerGetLoadableImage, (N1xDlaHandle compiler, const char *profile_name, void *out_buf), (compiler, profile_name, out_buf))

FORWARD_DLA(uint32_t, NvDlaNetworkAddInput, (N1xDlaHandle net, const char *name, N1xDlaDims4 dims, N1xDlaHandle *out_tensor), (net, name, dims, out_tensor))
FORWARD_DLA(uint32_t, NvDlaNetworkMarkOutput, (N1xDlaHandle net, N1xDlaHandle tensor), (net, tensor))
FORWARD_DLA(uint32_t, NvDlaNetworkGetNumInputs, (N1xDlaHandle net, uint32_t *out_count), (net, out_count))
FORWARD_DLA(uint32_t, NvDlaNetworkGetNumOutputs, (N1xDlaHandle net, uint32_t *out_count), (net, out_count))
FORWARD_DLA(uint32_t, NvDlaNetworkGetNumLayers, (N1xDlaHandle net, uint32_t *out_count), (net, out_count))

FORWARD_DLA(uint32_t, NvDlaNetworkAddConvolution,
            (N1xDlaHandle net, N1xDlaHandle input, int32_t num_outputs, int32_t pad_val,
             N1xDlaDims2 kernel, N1xDlaDims2 tl_pad, N1xDlaDims2 br_pad, N1xDlaDims2 stride, N1xDlaDims2 dilation,
             N1xDlaWeights kernel_w, N1xDlaWeights bias_w, uint8_t bias_mode, int32_t num_groups, N1xDlaHandle *out_layer),
            (net, input, num_outputs, pad_val, kernel, tl_pad, br_pad, stride, dilation, kernel_w, bias_w, bias_mode, num_groups, out_layer))

FORWARD_DLA(uint32_t, NvDlaNetworkAddDeconvolution,
            (N1xDlaHandle net, N1xDlaHandle input, int32_t num_outputs, int32_t pad_val,
             N1xDlaDims2 kernel, N1xDlaDims2 tl_pad, N1xDlaDims2 br_pad, N1xDlaDims2 stride, N1xDlaDims2 dilation,
             N1xDlaWeights kernel_w, N1xDlaWeights bias_w, uint8_t bias_mode, int32_t num_groups, N1xDlaHandle *out_layer),
            (net, input, num_outputs, pad_val, kernel, tl_pad, br_pad, stride, dilation, kernel_w, bias_w, bias_mode, num_groups, out_layer))

FORWARD_DLA(uint32_t, NvDlaNetworkAddFullyConnected,
            (N1xDlaHandle net, N1xDlaHandle input, int32_t num_outputs,
             N1xDlaWeights kernel_w, N1xDlaWeights bias_w, uint8_t bias_mode, N1xDlaHandle *out_layer),
            (net, input, num_outputs, kernel_w, bias_w, bias_mode, out_layer))

FORWARD_DLA(uint32_t, NvDlaNetworkAddActivation,
            (N1xDlaHandle net, N1xDlaHandle input, uint8_t act_type, N1xDlaHandle *out_layer),
            (net, input, act_type, out_layer))

FORWARD_DLA(uint32_t, NvDlaNetworkAddActivationWeights,
            (N1xDlaHandle net, N1xDlaHandle input, uint8_t act_type,
             N1xDlaWeights alpha, N1xDlaWeights beta, N1xDlaHandle *out_layer),
            (net, input, act_type, alpha, beta, out_layer))

FORWARD_DLA(uint32_t, NvDlaNetworkAddPooling,
            (N1xDlaHandle net, N1xDlaHandle input, uint8_t pool_type,
             N1xDlaDims2 window, N1xDlaDims2 stride, N1xDlaDims2 tl_pad, N1xDlaDims2 br_pad, N1xDlaHandle *out_layer),
            (net, input, pool_type, window, stride, tl_pad, br_pad, out_layer))

FORWARD_DLA(uint32_t, NvDlaNetworkAddElementWise,
            (N1xDlaHandle net, N1xDlaHandle input0, N1xDlaHandle input1, uint8_t op_type, N1xDlaHandle *out_layer),
            (net, input0, input1, op_type, out_layer))

FORWARD_DLA(uint32_t, NvDlaNetworkAddScale,
            (N1xDlaHandle net, N1xDlaHandle input, uint8_t mode,
             N1xDlaWeights shift, N1xDlaWeights scale, N1xDlaWeights power, N1xDlaHandle *out_layer),
            (net, input, mode, shift, scale, power, out_layer))

FORWARD_DLA(uint32_t, NvDlaNetworkAddBatchNorm,
            (N1xDlaHandle net, N1xDlaHandle input, uint8_t mode,
             N1xDlaWeights mean, N1xDlaWeights variance, float epsilon, N1xDlaHandle *out_layer),
            (net, input, mode, mean, variance, epsilon, out_layer))

FORWARD_DLA(uint32_t, NvDlaNetworkAddConcatenation,
            (N1xDlaHandle net, const N1xDlaHandle *inputs, int32_t num_inputs, N1xDlaHandle *out_layer),
            (net, inputs, num_inputs, out_layer))

FORWARD_DLA(uint32_t, NvDlaConcatenationLayerSetAxis,
            (N1xDlaHandle layer, int32_t axis),
            (layer, axis))

FORWARD_DLA(uint32_t, NvDlaNetworkAddSlice,
            (N1xDlaHandle net, N1xDlaHandle input,
             N1xDlaWeights starts, N1xDlaWeights ends, N1xDlaWeights axes,
             N1xDlaWeights steps, uint8_t mode, N1xDlaHandle *out_layer),
            (net, input, starts, ends, axes, steps, mode, out_layer))

FORWARD_DLA(uint32_t, NvDlaNetworkAddPixelShuffle,
            (N1xDlaHandle net, N1xDlaHandle input, uint8_t type, int32_t upscale_factor, N1xDlaHandle *out_layer),
            (net, input, type, upscale_factor, out_layer))

FORWARD_DLA(uint32_t, NvDlaNetworkAddResize,
            (N1xDlaHandle net, N1xDlaHandle input, uint8_t mode, N1xDlaWeights scales, N1xDlaHandle *out_layer),
            (net, input, mode, scales, out_layer))

FORWARD_DLA(uint32_t, NvDlaNetworkAddResizeOutputDims,
            (N1xDlaHandle net, N1xDlaHandle input, uint8_t mode, N1xDlaDims4 out_dims, uint8_t align_corners, N1xDlaHandle *out_layer),
            (net, input, mode, out_dims, align_corners, out_layer))

FORWARD_DLA(uint32_t, NvDlaNetworkAddQuantize,
            (N1xDlaHandle net, N1xDlaHandle input, N1xDlaWeights scales, N1xDlaWeights zero_points, int32_t axis, N1xDlaHandle *out_layer),
            (net, input, scales, zero_points, axis, out_layer))

FORWARD_DLA(uint32_t, NvDlaNetworkAddDequantize,
            (N1xDlaHandle net, N1xDlaHandle input, N1xDlaWeights scales, N1xDlaWeights zero_points, int32_t axis, N1xDlaHandle *out_layer),
            (net, input, scales, zero_points, axis, out_layer))

FORWARD_DLA(uint32_t, NvDlaNetworkAddSoftMax,
            (N1xDlaHandle net, N1xDlaHandle input, N1xDlaHandle *out_layer),
            (net, input, out_layer))

FORWARD_DLA(uint32_t, NvDlaNetworkAddSoftMaxAxis,
            (N1xDlaHandle net, N1xDlaHandle input, int32_t axis, N1xDlaHandle *out_layer),
            (net, input, axis, out_layer))

FORWARD_DLA(uint32_t, NvDlaNetworkAddUnary,
            (N1xDlaHandle net, N1xDlaHandle input, uint8_t unary_type, N1xDlaHandle *out_layer),
            (net, input, unary_type, out_layer))

FORWARD_DLA(uint32_t, NvDlaNetworkAddUnaryWeights,
            (N1xDlaHandle net, N1xDlaHandle input, uint8_t unary_type,
             N1xDlaWeights alpha, N1xDlaWeights beta, N1xDlaHandle *out_layer),
            (net, input, unary_type, alpha, beta, out_layer))

FORWARD_DLA(uint32_t, NvDlaNetworkAddReduce,
            (N1xDlaHandle net, N1xDlaHandle input, uint8_t reduce_op, N1xDlaWeights axes, N1xDlaHandle *out_layer),
            (net, input, reduce_op, axes, out_layer))

FORWARD_DLA(uint32_t, NvDlaNetworkAddNormalization,
            (N1xDlaHandle net, N1xDlaHandle input, N1xDlaWeights axes, float epsilon, N1xDlaHandle *out_layer),
            (net, input, axes, epsilon, out_layer))

FORWARD_DLA(uint32_t, NvDlaNetworkAddNormalizationExt,
            (N1xDlaHandle net, N1xDlaHandle input, N1xDlaWeights axes, float epsilon,
             N1xDlaDims4 param_dims, N1xDlaWeights scale, N1xDlaWeights bias, N1xDlaHandle *out_layer),
            (net, input, axes, epsilon, param_dims, scale, bias, out_layer))

FORWARD_DLA(uint32_t, NvDlaNormalizationLayerSetNormalizeMean,
            (N1xDlaHandle layer, uint8_t enable),
            (layer, enable))

FORWARD_DLA(uint32_t, NvDlaNormalizationLayerSetNormalizeVariance,
            (N1xDlaHandle layer, uint8_t enable),
            (layer, enable))

FORWARD_DLA(uint32_t, NvDlaNetworkAddMatMul,
            (N1xDlaHandle net, N1xDlaHandle input0, N1xDlaHandle input1, N1xDlaHandle *out_layer),
            (net, input0, input1, out_layer))

FORWARD_DLA(uint32_t, NvDlaNetworkAddReshape,
            (N1xDlaHandle net, N1xDlaHandle input, N1xDlaDims4 dims, N1xDlaHandle *out_layer),
            (net, input, dims, out_layer))

FORWARD_DLA(uint32_t, NvDlaNetworkAddTranspose,
            (N1xDlaHandle net, N1xDlaHandle input, N1xDlaDims4 perm, N1xDlaHandle *out_layer),
            (net, input, perm, out_layer))

FORWARD_DLA(uint32_t, NvDlaNetworkAddPad,
            (N1xDlaHandle net, N1xDlaHandle input, N1xDlaDims4 pre_pad, N1xDlaDims4 post_pad,
             uint8_t pad_mode, N1xDlaHandle *out_layer),
            (net, input, pre_pad, post_pad, pad_mode, out_layer))

FORWARD_DLA(uint32_t, NvDlaPadLayerSetConstantValue,
            (N1xDlaHandle layer, float val),
            (layer, val))

FORWARD_DLA(uint32_t, NvDlaNetworkAddTile,
            (N1xDlaHandle net, N1xDlaHandle input, N1xDlaDims4 repeats, N1xDlaHandle *out_layer),
            (net, input, repeats, out_layer))

FORWARD_DLA(uint32_t, NvDlaNetworkAddGather,
            (N1xDlaHandle net, N1xDlaHandle input, N1xDlaHandle indices,
             uint8_t mode, int32_t axis, N1xDlaHandle *out_layer),
            (net, input, indices, mode, axis, out_layer))

FORWARD_DLA(uint32_t, NvDlaNetworkAddSelect,
            (N1xDlaHandle net, N1xDlaHandle input0, N1xDlaHandle input1,
             N1xDlaWeights cond, N1xDlaDims4 cond_dims, N1xDlaHandle *out_layer),
            (net, input0, input1, cond, cond_dims, out_layer))

FORWARD_DLA(uint32_t, NvDlaNetworkAddTopK,
            (N1xDlaHandle net, N1xDlaHandle input, uint32_t k, uint32_t reduce_axes, N1xDlaHandle *out_layer),
            (net, input, k, reduce_axes, out_layer))

FORWARD_DLA(uint32_t, NvDlaNetworkAddLRN,
            (N1xDlaHandle net, N1xDlaHandle input, int32_t window,
             float alpha, float beta, float k, N1xDlaHandle *out_layer),
            (net, input, window, alpha, beta, k, out_layer))

FORWARD_DLA(uint32_t, NvDlaLayerGetOutput, (N1xDlaHandle layer, int32_t index, N1xDlaHandle *out_tensor), (layer, index, out_tensor))
FORWARD_DLA(uint32_t, NvDlaLayerGetInput, (N1xDlaHandle layer, int32_t index, N1xDlaHandle *out_tensor), (layer, index, out_tensor))
FORWARD_DLA(uint32_t, NvDlaLayerSetName, (N1xDlaHandle layer, const char *name), (layer, name))
FORWARD_DLA(uint32_t, NvDlaLayerSetComputePrecision, (N1xDlaHandle layer, uint8_t precision), (layer, precision))

FORWARD_DLA(uint32_t, NvDlaTensorSetName, (N1xDlaHandle tensor, const char *name), (tensor, name))
FORWARD_DLA(uint32_t, NvDlaTensorGetDimensions, (N1xDlaHandle tensor, N1xDlaDims4 *out_dims), (tensor, out_dims))
FORWARD_DLA(uint32_t, NvDlaTensorSetDataType, (N1xDlaHandle tensor, uint8_t dtype), (tensor, dtype))
FORWARD_DLA(uint32_t, NvDlaTensorGetDataType, (N1xDlaHandle tensor, uint8_t *out_dtype), (tensor, out_dtype))
FORWARD_DLA(uint32_t, NvDlaTensorSetDataFormat, (N1xDlaHandle tensor, uint8_t format), (tensor, format))
FORWARD_DLA(uint32_t, NvDlaTensorSetSurfaceFormat, (N1xDlaHandle tensor, uint8_t format), (tensor, format))
FORWARD_DLA(uint32_t, NvDlaTensorSetChannelDynamicRange, (N1xDlaHandle tensor, int32_t channel_idx, float min_val, float max_val), (tensor, channel_idx, min_val, max_val))
FORWARD_DLA(uint32_t, NvDlaTensorSetChannelOffset, (N1xDlaHandle tensor, int32_t channel_idx, float offset), (tensor, channel_idx, offset))

} /* extern "C" */
