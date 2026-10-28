#!/usr/bin/env python3
"""Exercise the real fops initializer against the running kernel's open gates."""
from pathlib import Path
import subprocess
import tempfile

ROOT=Path(__file__).resolve().parents[1]


def main():
    driver=(ROOT/'driver/n1x_dla_accel.c').read_text()
    initializer=driver[driver.index('static const struct file_operations accel_fops ='):driver.index('static const struct drm_ioctl_desc')]
    core=(ROOT/'results/linux-7.2.8-p16-npu2/drivers/gpu/drm/drm_file.c').read_text()
    gate=core[core.index('int drm_open_helper('):]
    gate=gate[:gate.index('\tdrm_dbg_core(')]+'return 0;\n}\n'
    shim=r'''
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#define THIS_MODULE 0
#define FOP_UNSIGNED_OFFSET 0x100
#define WARN_ON_ONCE(x) (x)
#define DRM_SWITCH_POWER_ON 1
#define DRM_SWITCH_POWER_DYNAMIC_OFF 2
static int accel_open(void){return 0;}
static int drm_release(void){return 0;}
static int drm_ioctl(void){return 0;}
static int drm_compat_ioctl(void){return 0;}
static int noop_llseek(void){return 0;}
static int drm_cpu_valid(void){return 1;}
struct file_operations {unsigned int fop_flags;void *owner;
 int (*open)(void),(*release)(void),(*unlocked_ioctl)(void),(*compat_ioctl)(void),(*llseek)(void);};
struct file {int f_flags;const struct file_operations *f_op;};
struct drm_device {int switch_power_state;};
struct drm_minor {struct drm_device *dev;};
struct drm_file;
'''
    check=r'''
int main(void) {
 struct drm_device dev={DRM_SWITCH_POWER_ON};struct drm_minor minor={&dev};
 struct file file={O_RDWR,&accel_fops};
 assert(drm_open_helper(&file,&minor)==0);
 struct file_operations broken=accel_fops;broken.fop_flags&=~FOP_UNSIGNED_OFFSET;file.f_op=&broken;
 assert(drm_open_helper(&file,&minor)==-EINVAL);
 file.f_op=&accel_fops;file.f_flags|=O_EXCL;assert(drm_open_helper(&file,&minor)==-EBUSY);
 return 0;
}
'''
    with tempfile.TemporaryDirectory(prefix='n1x-accel-open-') as tmp:
        source=Path(tmp)/'open.c';binary=Path(tmp)/'open'
        source.write_text(shim+initializer+gate+check)
        subprocess.run(['cc','-Wall','-Wextra','-Werror','-Wno-unused-variable',str(source),'-o',str(binary)],check=True)
        subprocess.run([str(binary)],check=True)
    print('Actual fops + kernel open gates: valid registration, missing flag and exclusive-open cases passed; no hardware')


if __name__=='__main__':main()
