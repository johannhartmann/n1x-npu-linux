#!/usr/bin/env python3
"""Exercise production context lookup, references and scoped DSP client leases."""
from pathlib import Path
import subprocess
import tempfile

from boot_test_context import context_layout

ROOT = Path(__file__).resolve().parents[1]


def main():
    source = (ROOT / 'driver/n1x_npu_boot_linux.c').read_text()
    registry = source[source.index('static void n1x_boot_release('):
                      source.index('static int n1x_boot_group_member(')]
    clients = source[source.index('static int n1x_sroot_client_ready('):
                     source.index('EXPORT_SYMBOL_GPL(n1x_sroot_client_ops_v3);')]
    matcher = source[source.index('static struct acpi_device *n1x_find_sroot('):source.index('static void n1x_match_put(')]
    diagnostics = source[source.index('static int n1x_debugfs_emit_attr('):source.index('static int n1x_debugfs_status_show(')]
    code = context_layout() + r'''
#include <stdlib.h>
#include <stddef.h>
#include "n1x_sroot_client.h"
#define READ_ONCE(x) (x)
#define WRITE_ONCE(x,v) ((x)=(v))
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
#define IS_ERR(p) ((uintptr_t)(p)>=(uintptr_t)-4095)
#define IS_ERR_OR_NULL(p) (!(p)||IS_ERR(p))
#define PTR_ERR(p) ((long)(intptr_t)(p))
#define ERR_PTR(e) ((void *)(intptr_t)(e))
#define ERR_CAST(p) ((void *)(p))
#define EPROBE_DEFER 517
#define GFP_KERNEL 0
#define kzalloc_obj(x) kzalloc(sizeof(x),GFP_KERNEL)
#define THIS_MODULE NULL
#define DL_FLAG_STATELESS 1
#define __free(name) __attribute__((cleanup(name)))
#define DEFINE_FREE(name,type,body) static void name(type *p){type _T=*p;body;}
#define EXPORT_SYMBOL_GPL(name)
struct fwnode_handle {struct fwnode_handle *parent;unsigned refs;};
struct device {struct fwnode_handle *node;unsigned refs;};
struct device_link {struct device *consumer,*supplier;};
static struct mutex n1x_contexts_lock;
static struct list_head n1x_contexts={&n1x_contexts,&n1x_contexts};
static unsigned allocations,modules,links,fault,uploads;
static struct n1x_boot_context *expected_upload;
static void mutex_lock(struct mutex *m) {assert(!m->value);m->value=1;}
static void mutex_unlock(struct mutex *m) {assert(m->value);m->value=0;}
static void *kzalloc(size_t bytes,int flags)
{assert(flags==GFP_KERNEL);if(fault==1)return NULL;allocations++;return calloc(1,bytes);}
static void kfree(void *p){if(p){assert(allocations);allocations--;free(p);}}
static struct device *get_device(struct device *d){if(d){assert(d->refs);d->refs++;}return d;}
static void put_device(struct device *d){if(d){assert(d->refs>1);d->refs--;}}
static struct fwnode_handle *dev_fwnode(struct device *d){return d->node;}
static struct fwnode_handle *fwnode_get_parent(struct fwnode_handle *f)
{if(f && f->parent)f->parent->refs++;return f?f->parent:NULL;}
static void fwnode_handle_put(struct fwnode_handle *f){if(f){assert(f->refs>1);f->refs--;}}
static void kref_get(struct kref *k){assert(k->value);k->value++;}
static void kref_put(struct kref *k,void (*release)(struct kref *))
{assert(k->value);if(--k->value==0)release(k);}
static bool try_module_get(void *m){(void)m;if(fault==2)return false;modules++;return true;}
static void module_put(void *m){(void)m;assert(modules);modules--;}
static struct device_link *device_link_add(struct device *c,struct device *s,unsigned flags)
{assert(!n1x_contexts_lock.value && flags==DL_FLAG_STATELESS);if(fault==3)return NULL;
 struct device_link *l=calloc(1,sizeof(*l));assert(l);l->consumer=c;l->supplier=s;links++;return l;}
static void device_link_del(struct device_link *l){assert(l && links);links--;free(l);}
static void list_add_tail(struct list_head *item,struct list_head *head)
{struct list_head *last=head->prev;item->next=head;item->prev=last;last->next=item;head->prev=item;}
static void list_del(struct list_head *item)
{((struct list_head *)item->prev)->next=item->next;((struct list_head *)item->next)->prev=item->prev;}
#define list_for_each_entry(pos,head,member) \
 for(struct list_head *it=(head)->next;it!=(head) && ((pos)=container_of(it,struct n1x_boot_context,member),1);it=it->next)
static int n1x_sroot_upload_dsp(struct n1x_boot_context *b,u32 id,const u8 *image,size_t bytes,
 struct n1x_sroot_client_result *r,bool release,bool *released)
{assert(b==expected_upload && id==0xc0304 && image && bytes==4 && release);
 uploads++;*r=(struct n1x_sroot_client_result){.reply_header=0x18,.published=1,.reply_valid=1,.accepted=1};
 *released=true;return 0;}
''' + registry + clients + r'''
#include <sys/types.h>
#define PAGE_SIZE 4096
struct device_attribute {int unused;};
struct seq_file {void *private;char output[32];};
struct inode {void *i_private;};
struct file {void *private_data;};
static int single_open(struct file *f,int (*show)(struct seq_file *,void *),void *data)
{(void)show;if(fault==4)return -ENOMEM;struct seq_file *m=calloc(1,sizeof(*m));assert(m);
 m->private=data;f->private_data=m;return 0;}
static int single_release(struct inode *i,struct file *f)
{(void)i;free(f->private_data);f->private_data=NULL;return 0;}
static void seq_write(struct seq_file *m,const char *p,size_t n)
{assert(n<sizeof(m->output));memcpy(m->output,p,n);}
static ssize_t show_attr(struct device *dev,struct device_attribute *attr,char *p)
{assert(!attr);struct n1x_boot_context *b=n1x_boot_get(dev);assert(b);
 n1x_boot_put_ref(b);return sprintf(p,"live");}
''' + diagnostics + r'''
struct acpi_device {struct acpi_device *parent;unsigned refs,revision;};
static struct acpi_device acpi_nodes[3];
static struct acpi_device *acpi_dev_parent(struct acpi_device *d){return d->parent;}
static struct acpi_device *acpi_dev_get(struct acpi_device *d){d->refs++;return d;}
static void acpi_dev_put(struct acpi_device *d){if(d){assert(d->refs>1);d->refs--;}}
static struct acpi_device *acpi_next(struct acpi_device *prev)
{unsigned start=prev?(unsigned)(prev-acpi_nodes+1):0;acpi_dev_put(prev);
 for(unsigned i=start;i<3;i++)if(acpi_nodes[i].revision==1)return acpi_dev_get(&acpi_nodes[i]);return NULL;}
#define for_each_acpi_dev_match(pos,hid,uid,rev) \
 for((pos)=acpi_next(NULL);(pos);(pos)=acpi_next(pos))
''' + matcher + r'''
static void check_matcher(void)
{
 struct acpi_device parent={0},other={0},consumer={.parent=&parent};
 for(unsigned i=0;i<3;i++)acpi_nodes[i]=(struct acpi_device){.parent=&other,.refs=1,.revision=1};
 assert(PTR_ERR(n1x_find_sroot(&consumer))==-ENODEV);
 acpi_nodes[1].parent=&parent;
 struct acpi_device *found=n1x_find_sroot(&consumer);assert(found==&acpi_nodes[1]);acpi_dev_put(found);
 acpi_nodes[2].parent=&parent;assert(PTR_ERR(n1x_find_sroot(&consumer))==-ENOTUNIQ);
 acpi_nodes[2].revision=2;found=n1x_find_sroot(&consumer);assert(found==&acpi_nodes[1]);acpi_dev_put(found);
 consumer.parent=NULL;assert(PTR_ERR(n1x_find_sroot(&consumer))==-ENODEV);
 for(unsigned i=0;i<3;i++)assert(acpi_nodes[i].refs==1);
}


static struct n1x_boot_context *make_boot(struct n1x_boot_endpoint *s,struct n1x_boot_endpoint *d)
{
 struct n1x_boot_context *b=kzalloc(sizeof(*b),GFP_KERNEL);assert(b);
 b->refs.value=1;b->dev=get_device(s->dev);b->consumer=get_device(d->dev);
 b->scope=fwnode_get_parent(dev_fwnode(s->dev));b->sroot=s;b->dla=d;
 b->upload_attempted=true;b->upload_result.reply_valid=true;b->registered=true;
 list_add_tail(&b->entry,&n1x_contexts);return b;
}
int main(void)
{
 check_matcher();
 struct fwnode_handle scope={.refs=1},other={.refs=1};
 struct fwnode_handle n1={.parent=&scope,.refs=1},n2={.parent=&other,.refs=1};
 struct device sdev={&n1,1},ddev={&n1,1},cdev={&n1,1};
 struct device sdev2={&n2,1},ddev2={&n2,1},cdev2={&n2,1},bad={NULL,1};
 struct n1x_boot_endpoint s={.dev=&sdev},d={.dev=&ddev};
 struct n1x_boot_endpoint s2={.dev=&sdev2},d2={.dev=&ddev2};
 const struct n1x_sroot_client_ops *ops=n1x_sroot_client_ops_v3();
 struct n1x_sroot_client *c,*c2;
 struct n1x_boot_context *b,*b2,*got;
 struct n1x_sroot_client_result_v2 result;
 u8 image[4]={};
 assert(PTR_ERR(n1x_boot_get_unique())==-EPROBE_DEFER);
 assert(PTR_ERR(ops->get(&bad))==-EINVAL);
 assert(PTR_ERR(ops->get(&cdev))==-EPROBE_DEFER);
 b=make_boot(&s,&d);
 got=n1x_boot_get(&sdev);assert(got==b && b->refs.value==2);n1x_boot_put_ref(got);
 got=n1x_boot_get(&ddev);assert(got==b);n1x_boot_put_ref(got);
 assert(!n1x_boot_get(&cdev));
 got=n1x_boot_get_unique();assert(got==b);n1x_boot_put_ref(got);
 assert(PTR_ERR(ops->get(&cdev2))==-EPROBE_DEFER);
 for(fault=1;fault<=3;fault++) {
  assert(IS_ERR(ops->get(&cdev)));assert(allocations==1 && !modules && !links);
  assert(b->refs.value==1 && scope.refs==2 && ddev.refs==2 && cdev.refs==1);
 }
 fault=0;
 b->upload_attempted=false;assert(PTR_ERR(ops->get(&cdev))==-EPROBE_DEFER);b->upload_attempted=true;
 put_device(b->consumer);b->consumer=NULL;
 assert(PTR_ERR(ops->get(&cdev))==-EPROBE_DEFER);b->consumer=get_device(&ddev);
 b2=make_boot(&s2,&d2);
 assert(PTR_ERR(n1x_boot_get_unique())==-ENOTUNIQ);
 struct seq_file global={0};assert(n1x_debugfs_emit_attr(&global,show_attr)==-ENOTUNIQ);
 c=ops->get(&cdev);c2=ops->get(&cdev2);assert(!IS_ERR(c) && !IS_ERR(c2));
 assert(c->boot==b && c2->boot==b2 && c->supplier==&ddev && c2->supplier==&ddev2);
 assert(ops->publish(c)==-EINVAL && !b->client_leases);
 d.dev=&ddev2;assert(ops->prepare(c)==-ENODEV && !b->client_leases);d.dev=&ddev;
 assert(ops->upload(c,0xc0304,image,4,&result)==-EPERM && !uploads);
 assert(!ops->prepare(c) && b->client_leases==1 && !b2->client_leases);
 assert(ops->prepare(c)==-EALREADY && b->client_leases==1);
 d.dev=&ddev2;assert(ops->publish(c)==-ENODEV && b->client_leases==1);d.dev=&ddev;
 assert(!ops->prepare(c2) && b2->client_leases==1);
 b->sleep_prepared=true;assert(ops->publish(c)==-EBUSY);b->sleep_prepared=false;
 assert(!ops->cancel(c) && !b->client_leases);assert(!ops->cancel(c));
 for(unsigned f=0;f<5;f++) {
  b->sleep_prepared=f==0;b->stopping=f==1;b->loader_poisoned=f==2;
  b->upload_error=f==3?-EIO:0;b->removing=f==4;
  assert(ops->prepare(c)<0 && !b->client_leases);
 }
 b->sleep_prepared=b->stopping=b->loader_poisoned=b->removing=false;b->upload_error=0;
 assert(!ops->prepare(c) && !ops->publish(c));assert(ops->publish(c)==-EALREADY);
 expected_upload=b;assert(!ops->upload(c,0xc0304,image,4,&result));
 assert(uploads==1 && result.accepted && result.staging_released);
 assert(ops->cancel(c)==-EBUSY && ops->put(c)==-EBUSY);
 assert(b->client_leases==1 && b->refs.value==2 && modules==2 && links==2);
 /* Test teardown only: the production API deliberately has no such release. */
 c->published=false;assert(!ops->put(c));
 assert(!ops->put(c2) && !b2->client_leases);
 /* Ambiguous siblings fail, even when the second DLA is unbound. */
 fwnode_handle_put(b2->scope);b2->scope=&scope;scope.refs++;
 put_device(b2->consumer);b2->consumer=NULL;
 assert(PTR_ERR(ops->get(&cdev))==-ENOTUNIQ && !links && !modules);
 n1x_boot_owner_put(b2);
 /* Handle references survive provider unregister, but cannot admit work. */
 c=ops->get(&cdev);assert(!IS_ERR(c));
 struct inode inode={.i_private=&sdev};struct file file={0};
 unsigned device_refs=sdev.refs;
 fault=4;assert(n1x_debugfs_open(&inode,&file,NULL)==-ENOMEM && sdev.refs==device_refs);fault=0;
 assert(!n1x_debugfs_open(&inode,&file,NULL) && sdev.refs==device_refs+1);
 struct seq_file *diagnostic=file.private_data;
 assert(!n1x_debugfs_emit_attr(diagnostic,show_attr) && !strcmp(diagnostic->output,"live"));
 n1x_boot_owner_put(b);
 assert(n1x_debugfs_emit_attr(diagnostic,show_attr)==-ENODEV);
 assert(!n1x_debugfs_release(&inode,&file));
 assert(b->refs.value==1 && ops->prepare(c)==-ENODEV);
 assert(PTR_ERR(ops->get(&cdev))==-EPROBE_DEFER);
 assert(!ops->put(c) && !allocations && !links && !modules);
 assert(scope.refs==1 && other.refs==1 && sdev.refs==1 && ddev.refs==1 && cdev.refs==1);
 assert(sdev2.refs==1 && ddev2.refs==1 && cdev2.refs==1);
 b=make_boot(&s,&d);n1x_boot_hold_publication(b);n1x_boot_hold_publication(b);
 assert(b->refs.value==2 && b->dma_dev==&ddev && ddev.refs==3);
 /* A failed-stop component detach drops its bound-consumer reference only. */
 put_device(b->consumer);b->consumer=NULL;
 n1x_boot_owner_put(b);assert(b->refs.value==1 && b->removing && ddev.refs==2);
 /* Teardown model only: this test has no DMA; production requires a checked stop. */
 n1x_boot_drop_publication(b);assert(!allocations && ddev.refs==1 && sdev.refs==1);
 puts("Production NPU contexts: scoped isolation, ambiguity, client admission/publication, probe unwinding, detach and reference lifetime: PASS");
}
'''
    with tempfile.TemporaryDirectory(prefix='n1x-context-') as temporary:
        test, binary = Path(temporary)/'context.c', Path(temporary)/'context'
        test.write_text(code)
        subprocess.run(['cc', '-I'+str(ROOT/'include'), '-std=gnu11', '-Wall', '-Wextra',
                        '-Werror', '-Wno-unused-function', '-Wno-misleading-indentation', '-fsanitize=address,undefined',
                        '-g', str(test), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True, timeout=30)


if __name__ == '__main__':
    main()
