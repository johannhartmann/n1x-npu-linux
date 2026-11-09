"""Kernel-edge shims sharing the production bootstrap context layout."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FIELDS = {
    'boot_waitq': 'waitq', 'boot_irq_count': 'irq_count',
    'boot_irq_registered': 'irq_registered', 'boot_lock': 'lock.value',
    'boot_dla': 'dla', 'boot_sroot': 'sroot', 'boot_result': 'upload_result',
    'boot_bytes': 'image_bytes', 'boot_buffer': 'image', 'boot_dma': 'image_dma',
    'boot_error': 'upload_error', 'boot_attempted': 'upload_attempted',
    'boot_stopping': 'stopping', 'boot_hfrp_held': 'hfrp_held',
    'boot_upload_held': 'upload_held', 'boot_sleep_prepared': 'sleep_prepared',
    'boot_shutdown': 'shutdown', 'boot_hfrp': 'hfrp', 'boot_ping': 'ping',
    'boot_config': 'config', 'boot_region5': 'region5',
    'boot_config_apply': 'config_apply', 'boot_lpwr': 'lpwr',
    'boot_task': 'task', 'boot_queue': 'queue', 'boot_preempt': 'preempt',
    'boot_suspended': 'suspended', 'sroot_client_poisoned': 'loader_poisoned',
    'sroot_dsp_buffers': 'dsp_buffers',
}


def context_layout():
    source = (ROOT / 'driver/n1x_npu_boot_linux.c').read_text()
    layout = source[source.index('struct n1x_boot_context;'):
                    source.index('static DEFINE_MUTEX(n1x_contexts_lock);')]
    prefix = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
#include "n1x_dla_boot.h"
#include "n1x_dla_control.h"
#include "n1x_dla_job.h"
#include "n1x_hfrp_setup.h"
typedef uint8_t u8;
typedef uint32_t u32;
typedef __u64 u64;
typedef __u64 dma_addr_t;
typedef size_t resource_size_t;
typedef int wait_queue_head_t;
typedef int atomic_t;
#define __iomem
struct mutex {int value;};
struct kref {_Atomic unsigned value;};
struct list_head {void *next,*prev;};
'''
    return prefix + layout


def context_harness():
    aliases = ''.join(f'#define {name} (boot->{field})\n' for name, field in FIELDS.items())
    return context_layout() + r'''
static struct mutex n1x_contexts_lock __attribute__((unused));
static int test_boot_lookup_error;
static _Atomic unsigned test_boot_lookups;
static struct n1x_boot_context test_boot;
static struct n1x_boot_context *boot = &test_boot;
#define IS_ERR(p) ((uintptr_t)(p)>=(uintptr_t)-4095)
#define PTR_ERR(p) ((long)(intptr_t)(p))
static inline void n1x_boot_put_ref(struct n1x_boot_context *value)
{assert(value && value->refs.value);value->refs.value--;}
static inline void test_boot_cleanup(struct n1x_boot_context **value)
{if(*value && !IS_ERR(*value))n1x_boot_put_ref(*value);}
#define __free(name) __attribute__((cleanup(test_boot_cleanup)))
static inline struct n1x_boot_context *n1x_boot_get(struct device *dev)
{(void)dev;boot->refs.value++;return boot;}
static inline struct n1x_boot_context *n1x_boot_get_unique(void)
{test_boot_lookups++;if(test_boot_lookup_error)return (void *)(intptr_t)test_boot_lookup_error;
 boot->refs.value++;return boot;}
static inline void n1x_boot_hold_publication(struct n1x_boot_context *value)
{if(!value->lifetime_held){value->refs.value++;value->lifetime_held=true;}}
static inline void n1x_boot_drop_publication(struct n1x_boot_context *value)
{if(value->lifetime_held){value->lifetime_held=false;n1x_boot_put_ref(value);}}
static inline void test_context_reset(void)
{test_boot_lookup_error=0;test_boot_lookups=0;assert(!boot->lock.value);
 assert(!boot->refs.value || boot->refs.value==1+(unsigned)boot->lifetime_held);
 memset(boot,0,sizeof(*boot));boot->refs.value=1;}
''' + aliases
