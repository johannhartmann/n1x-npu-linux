/* SPDX-License-Identifier: GPL-2.0-only */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "n1x_dla_firmware.h"
static unsigned char fw[1536];
static void put(size_t off, unsigned value)
{ for (int i=0;i<4;i++) fw[off+i]=(unsigned char)(value>>(8*i)); }
static void valid(void)
{
 memset(fw,0,sizeof(fw));put(0,0x10fe);put(4,1);put(8,sizeof(fw));
 put(12,256);put(16,512);put(20,1024);put(256,1);
 put(0x128,512);put(0x12c,512);put(0x130,0);put(0x134,512);
}
int main(int argc,char **argv)
{
 struct n1x_dla_firmware out;
 valid();assert(!n1x_dla_parse_firmware(fw,sizeof(fw),&out));
 assert(out.monitor_code_size==512 && out.monitor_data_offset==512);
 const unsigned bad[][2]={{0,0},{4,2},{8,1537},{12,0xfffffff0},
 {16,0xfffffff0},{20,0xffffffff},{256,2},{0x128,0xffffffff},
 {0x12c,0xffffffff},{0x130,256},{0x134,513}};
 for(size_t i=0;i<sizeof(bad)/sizeof(bad[0]);i++){
  valid();put(bad[i][0],bad[i][1]);memset(&out,0xa5,sizeof(out));
  struct n1x_dla_firmware saved=out;
  assert(n1x_dla_parse_firmware(fw,sizeof(fw),&out)<0);
  assert(!memcmp(&out,&saved,sizeof(out)));
 }
 assert(n1x_dla_parse_firmware(NULL,0,&out)<0);
 assert(n1x_dla_parse_firmware(fw,23,&out)<0);
 assert(n1x_dla_parse_firmware(fw,sizeof(fw),NULL)<0);
 if(argc==2){
  FILE *f=fopen(argv[1],"rb");assert(f);assert(!fseek(f,0,SEEK_END));long n=ftell(f);assert(n>0&&n<1048576);rewind(f);
  unsigned char *b=malloc(n);assert(b);assert(fread(b,1,n,f)==(size_t)n);fclose(f);
  assert(!n1x_dla_parse_firmware(b,n,&out));
  assert(out.payload_offset==512&&out.monitor_code_size==44800&&out.monitor_data_size==41984);
  free(b);
 }
 puts("firmware validation: synthetic bounds cases and optional real image passed");
 return 0;
}
