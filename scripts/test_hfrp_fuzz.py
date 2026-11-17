#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Seeded malformed HFRP ring tests against the actual C reply consumer."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]

HARNESS = r'''
#include <assert.h>
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "n1x_hfrp.h"
static uint8_t ram[512];
static unsigned writes;
static uint64_t state;
static uint64_t draw(void)
{state^=state<<13;state^=state>>7;state^=state<<17;return state;}
static uint32_t word(unsigned off)
{uint32_t value;memcpy(&value,ram+off,4);return value;}
static void put(unsigned off,uint32_t value) {memcpy(ram+off,&value,4);}
static int read32(void *context,uint32_t off,uint32_t *value)
{assert(context==ram && !(off&3) && off<=sizeof(ram)-4);
 *value=word(off);return 0;}
static int write32(void *context,uint32_t off,uint32_t value)
{assert(context==ram && off==0x110);writes++;put(off,value);return 0;}
int main(void)
{
 const uint64_t seeds[]={1,0x200a,0x20251722,0x61742deadbeefULL};
 struct n1x_dla_io io={ram,write32,read32,NULL};
 unsigned accepted=0,rejected=0,wrapped=0;
 for(unsigned seed=0;seed<sizeof(seeds)/sizeof(seeds[0]);seed++) {
  state=seeds[seed];
  for(unsigned iteration=0;iteration<8192;iteration++) {
   for(unsigned i=0;i<sizeof(ram);i++)ram[i]=draw();
   unsigned tail=draw()%116,available=draw()%116;
   unsigned head=(tail+available)%116;
   uint32_t client=(uint32_t)draw(),server=(uint32_t)draw();
   client=(client&~0xff00U)|(tail<<8);server=(server&~0xffU)|head;
   uint32_t header=(uint32_t)draw();
   if(iteration%2)header=(header&~255U)|(4+draw()%51);
   for(unsigned i=0;i<4;i++)ram[0x18c+(tail+i)%116]=header>>(8*i);
   if(iteration%13==0) {tail=116+draw()%140;client=(client&~0xff00U)|(tail<<8);}
   else if(iteration%17==0) {head=116+draw()%140;server=(server&~0xffU)|head;}
   put(0x110,client);put(0x188,server);writes=0;
   struct {uint64_t before;struct n1x_hfrp_reply reply;uint64_t after;} out;
   memset(&out,0xa5,sizeof(out));out.before=0x1122334455667788ULL;out.after=~out.before;
   int expected;
   unsigned total=header&255;
   if(tail>=116 || head>=116)expected=-EPROTO;
   else if(available<4)expected=-EAGAIN;
   else if(total<4)expected=-EPROTO;
   else if(total>54)expected=-EMSGSIZE;
   else if(total>available)expected=-EAGAIN;
   else expected=0;
   int result=n1x_hfrp_read_reply(&io,&out.reply);
   if(result!=expected) {
    fprintf(stderr,"seed=%llu iteration=%u result=%d expected=%d\n",
     (unsigned long long)seeds[seed],iteration,result,expected);return 1;
   }
   assert(out.before==0x1122334455667788ULL && out.after==~out.before);
   assert(word(0x188)==server);
   if(result) {rejected++;assert(!writes && word(0x110)==client);}
   else {
    accepted++;assert(writes==1 && out.reply.tail_publish_attempted && out.reply.tail_published);
    assert(word(0x110)==((client&~0xff00U)|(((tail+total)%116)<<8)));
    assert(out.reply.header==header && out.reply.length==total-4);
    assert(out.reply.sequence==((header>>8)&1023) && out.reply.status==((header>>18)&1023));
    for(unsigned i=0;i<total-4;i++)assert(out.reply.payload[i]==ram[0x18c+(tail+4+i)%116]);
    if(tail+total>116)wrapped++;
   }
  }
 }
 printf("Seeded HFRP reply properties: 32768 cases, %u accepted, %u rejected, %u wrapped; ASan/UBSan, no hardware\n",
  accepted,rejected,wrapped);
}
'''


def main():
    with tempfile.TemporaryDirectory(prefix='n1x-hfrp-fuzz-') as tmp:
        source, binary = Path(tmp) / 'test.c', Path(tmp) / 'test'
        source.write_text(HARNESS)
        subprocess.run(shlex.split(os.environ.get('CC', 'cc')) + [
            '-O1', '-g', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined',
            '-fno-sanitize-recover=all', '-fno-omit-frame-pointer',
            '-I', str(ROOT / 'include'), str(source), str(ROOT / 'driver/n1x_hfrp.c'),
            '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)


if __name__ == '__main__':
    main()
