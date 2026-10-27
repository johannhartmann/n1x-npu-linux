#!/usr/bin/env python3
"""Check real C client ABI and syscall/error behavior without opening a device."""
import ctypes
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'runtime'))
from n1x_dla import Info, Result, Range


def main():
    code = r'''
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "n1x_dla.h"
static int mode,calls,closes;
int __wrap_open(const char *path,int flags,...)
{assert(!strcmp(path,"/dev/test")&&(flags&O_CLOEXEC));return 73;}
int __wrap_close(int fd) {assert(fd==73);closes++;return 0;}
int __wrap_ioctl(int fd,unsigned long request,...)
{
 assert(fd==73);calls++;va_list ap;va_start(ap,request);void *data=va_arg(ap,void *);va_end(ap);
 if(request==DRM_IOCTL_VERSION){struct drm_version *v=data;
  const char *name=mode==1?"another":"n1x-dla";strcpy(v->name,name);v->name_len=strlen(name);return 0;}
 if(request==DRM_IOCTL_N1X_DLA_INFO){struct n1x_dla_accel_info *i=data;
  i->abi_version=mode==2?999:1;return 0;}
 if(mode==3){errno=EINTR;return -1;}
 if(request==DRM_IOCTL_N1X_DLA_EXEC){struct n1x_dla_accel_exec *r=data;
  assert(r->image_ptr&&r->ranges_ptr&&r->image_bytes==16&&r->num_addresses==1);
  assert(!r->job_id&&!r->status&&!r->completed&&!r->flags&&!r->reserved);
  r->job_id=8;r->status=mode==4?-ETIMEDOUT:0;r->completed=mode==5?0:1;return 0;}
 assert(request==DRM_IOCTL_N1X_DLA_READ);
 struct n1x_dla_accel_read *r=data;assert(r->job_id==8&&r->offset==3&&r->bytes==1&&!r->reserved);
 *(unsigned char *)(uintptr_t)r->data_ptr=0x5a;return 0;
}
int main(void)
{
 assert(n1x_dla_open("/dev/test")==73&&calls==2&&!closes);
 mode=1;assert(n1x_dla_open("/dev/test")==-ENODEV&&calls==3&&closes==1);
 mode=2;assert(n1x_dla_open("/dev/test")==-EPROTONOSUPPORT&&calls==5&&closes==2);
 unsigned char image[16]={0},out=0;
 struct n1x_dla_accel_range range={0,16};struct n1x_dla_accel_exec result;
 for(mode=3;mode<=5;mode++){
  int before=calls;int status=n1x_dla_submit(73,image,16,&range,1,&result);
  assert(calls==before+1&&status==(mode==3?-EINTR:mode==4?-ETIMEDOUT:-EPROTO));
  assert(result.job_id==(mode==3?0:8));
 }
 mode=0;assert(!n1x_dla_submit(73,image,16,&range,1,&result)&&result.job_id==8);
 assert(!n1x_dla_read(73,8,3,&out,1)&&out==0x5a);
 mode=3;int before=calls;assert(n1x_dla_read(73,8,3,&out,1)==-EINTR&&calls==before+1);
 assert(n1x_dla_open(NULL)==-EINVAL&&n1x_dla_info(73,NULL)==-EINVAL);
 assert(n1x_dla_submit(73,NULL,16,&range,1,&result)==-EINVAL);
 assert(n1x_dla_read(73,8,0,&out,0)==-EINVAL);
 printf("{\"info\":%zu,\"result\":%zu,\"range\":%zu,\"job_offset\":%zu,\"status_offset\":%zu}\n",
 sizeof(struct n1x_dla_accel_info),sizeof(result),sizeof(range),offsetof(struct n1x_dla_accel_exec,job_id),offsetof(struct n1x_dla_accel_exec,status));
}
'''
    with tempfile.TemporaryDirectory(prefix='n1x-runtime-') as tmp:
        source, binary = Path(tmp)/'test.c', Path(tmp)/'test'
        source.write_text(code)
        subprocess.run(shlex.split(os.environ.get('CC', 'cc')) + [
            '-O2', '-Wall', '-Wextra', '-Werror', '-I'+str(ROOT/'include'),
            '-I'+str(ROOT/'runtime'), str(source), str(ROOT/'runtime/n1x_dla.c'),
            '-Wl,--wrap=open,--wrap=close,--wrap=ioctl', '-o', str(binary)], check=True)
        layout = json.loads(subprocess.check_output([str(binary)], text=True))
    assert layout == dict(info=ctypes.sizeof(Info), result=ctypes.sizeof(Result),
                         range=ctypes.sizeof(Range), job_offset=Result.job_id.offset,
                         status_offset=Result.status.offset)
    print('C/Python ABI layout, driver/version checks, execution errors and no-retry semantics passed; no hardware access')


if __name__ == '__main__':
    main()
