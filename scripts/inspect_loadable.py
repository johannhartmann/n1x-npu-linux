#!/usr/bin/env python3
"""Bounded FlatBuffers inspection with an explicit NVDLA-v1 field projection.

The modern 0.8.0 schema is not claimed identical. Unknown fields are retained
as field offsets and no binary is submitted to a device.
"""
import argparse,json,struct,hashlib
from pathlib import Path
class Buffer:
 def __init__(self,b):self.b=b
 def u(self,fmt,at):
  n=struct.calcsize(fmt)
  if at<0 or at+n>len(self.b):raise ValueError('out of bounds scalar')
  return struct.unpack_from(fmt,self.b,at)[0]
 def field(self,t,index):
  v=t-self.u('<i',t);size=self.u('<H',v);n=self.u('<H',v+2)
  if size<4 or size%2 or t+n>len(self.b):raise ValueError('bad vtable')
  off=self.u('<H',v+4+2*index) if 4+2*index<size else 0
  if off and off>=n:raise ValueError('bad field offset')
  return t+off if off else None
 def scalar(self,t,i,fmt,default=0):
  f=self.field(t,i);return self.u(fmt,f) if f is not None else default
 def vector(self,t,i,width=4):
  f=self.field(t,i)
  if f is None:return []
  v=f+self.u('<I',f);n=self.u('<I',v)
  if n>len(self.b)//width or v+4+width*n>len(self.b):raise ValueError('bad vector')
  return range(v+4,v+4+width*n,width)
 def tables(self,t,i):return [p+self.u('<I',p) for p in self.vector(t,i)]
 def string(self,t,i):
  f=self.field(t,i)
  if f is None:return None
  p=f+self.u('<I',f);n=self.u('<I',p)
  if n>4096 or p+4+n>=len(self.b) or self.b[p+4+n]!=0:raise ValueError('bad string')
  return self.b[p+4:p+4+n].decode('utf-8')
 def strings(self,t,i):
  result=[]
  for f in self.vector(t,i):
   p=f+self.u('<I',f);n=self.u('<I',p)
   if n>4096 or p+4+n>=len(self.b) or self.b[p+4+n]!=0:raise ValueError('bad string vector entry')
   result.append(self.b[p+4:p+4+n].decode('utf-8'))
  return result
 def shortvec(self,t,i):return [self.u('<H',p) for p in self.vector(t,i,2)]

def inspect(data):
 b=Buffer(data);assert data[4:8]==b'NVDA';r=b.u('<I',0);f=b.field(r,0)
 result={'identifier':'NVDA','version_bytes':list(data[f:f+3]),'sha256':hashlib.sha256(data).hexdigest(),'interpretation':'NVDLA v1 schema projection; modern extensions unverified','root_vector_lengths':{str(i):len(b.vector(r,i)) for i in range(1,9)}}
 result['tasks']=[dict(id=b.scalar(t,0,'<H'),interface=b.scalar(t,1,'<I'),instance=b.scalar(t,2,'<h'),address_ids=b.shortvec(t,3),pre_actions=b.shortvec(t,4),post_actions=b.shortvec(t,5)) for t in b.tables(r,1)]
 result['memory']=[dict(id=b.scalar(t,0,'<H'),domain=b.scalar(t,1,'<B'),flags=b.scalar(t,2,'<H'),size=b.scalar(t,3,'<Q'),alignment=b.scalar(t,4,'<I'),content_count=len(b.vector(t,5)),contents=b.strings(t,5),content_offsets=[b.u('<Q',p) for p in b.vector(t,6,8)]) for t in b.tables(r,2)]
 result['addresses']=[dict(id=b.scalar(t,0,'<H'),memory_id=b.scalar(t,1,'<H'),offset=b.scalar(t,2,'<Q'),size=b.scalar(t,3,'<Q')) for t in b.tables(r,3)]
 result['blobs']=[dict(name=b.string(t,0),size=b.scalar(t,1,'<Q'),data_bytes=len(b.vector(t,5,1))) for t in b.tables(r,5)]
 result['submits']=[dict(id=b.scalar(t,0,'<H'),task_ids=b.shortvec(t,1)) for t in b.tables(r,8)]
 return result
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('loadable',type=Path);a=p.parse_args();print(json.dumps(inspect(a.loadable.read_bytes()),indent=2))
