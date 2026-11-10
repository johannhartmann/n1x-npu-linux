#!/usr/bin/env python3
"""Exercise the actual resume firmware cache without file or hardware access."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    text = (ROOT/'driver/n1x_npu_boot_linux.c').read_text()
    function = text[text.index('static int n1x_boot_cache_firmware('):
                    text.index('static int n1x_boot_upload(')]
    code = r'''
#include <assert.h>
#include <stdbool.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
#define N1X_BOOT_FIRMWARE "primary"
#define N1X_BOOT_FALLBACK_FIRMWARE "fallback"
struct firmware {int unused;};
struct device {int unused;};
struct endpoint {struct device *dev;};
struct n1x_boot_context {const struct firmware *firmware;bool sleep_prepared;struct endpoint *sroot;};
static struct device dev;
static struct endpoint ep={&dev};
static struct firmware image;
static unsigned firmware_variant,requests,validations,releases;
static int read_error,validate_error;
static const char *override;
static int device_property_read_string(struct device *d,const char *key,const char **name)
{assert(d==&dev && !strcmp(key,"firmware-name"));if(override)*name=override;return override?0:-EINVAL;}
static int request_firmware_direct(const struct firmware **f,const char *name,struct device *d)
{assert(d==&dev && !strcmp(name,override?override:firmware_variant?"fallback":"primary"));
 requests++;if(read_error)return read_error;*f=&image;return 0;}
static int n1x_boot_validate_firmware(const struct firmware *f)
{assert(f==&image);validations++;return validate_error;}
static void release_firmware(const struct firmware *f){assert(f==&image);releases++;}
''' + function + r'''
int main(void)
{
 for(unsigned variant=0;variant<2;variant++)for(unsigned custom=0;custom<2;custom++) {
  struct n1x_boot_context b={.sroot=&ep};firmware_variant=variant;override=custom?"custom":NULL;
  requests=validations=releases=0;
  b.sleep_prepared=true;
  assert(n1x_boot_cache_firmware(&b)==-EHOSTDOWN && !requests && !b.firmware);
  b.sleep_prepared=false;read_error=-ENOENT;
  assert(n1x_boot_cache_firmware(&b)==-ENOENT && requests==1 && !validations && !releases);
  read_error=0;validate_error=-EBADMSG;
  assert(n1x_boot_cache_firmware(&b)==-EBADMSG && requests==2 && validations==1 && releases==1);
  assert(!b.firmware);validate_error=0;
  assert(!n1x_boot_cache_firmware(&b) && b.firmware==&image && requests==3 && validations==2);
  b.sleep_prepared=true;read_error=-EIO;
  for(unsigned i=0;i<32;i++) assert(!n1x_boot_cache_firmware(&b));
  assert(requests==3 && validations==2 && releases==1 && b.firmware==&image);
  release_firmware(b.firmware);read_error=0;
 }
 puts("Firmware cache: variants, overrides, validation faults and 32 filesystem-free resume lookups: PASS");
}
'''
    with tempfile.TemporaryDirectory(prefix='n1x-firmware-cache-') as tmp:
        source, binary = Path(tmp)/'test.c', Path(tmp)/'test'
        source.write_text(code)
        subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined',
                        str(source), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)


if __name__ == '__main__':
    main()
