// Read-only component-9 diagnostic retrieval, not a raw-ROM guarantee.
// Allocation/free creates only process-local RM handles. No GPU reset, firmware
// upload, physical mapping or control writes. A successful dump may be metadata.
// Build using the matching NVIDIA 615.78.08 SDK and Unix include directories.
#include <sys/ioctl.h>
#include <fcntl.h>
#include <unistd.h>
#include <cerrno>
#include <cstdio>
#include <cstring>
#include <stdexcept>
#include <fstream>
#include <vector>
#include <string>
#include <nv-ioctl.h>
#include <nv_escape.h>
#include <nvos.h>
#include <nvstatus.h>
#include <class/cl0080.h>
#include <class/cl2080.h>
#include <ctrl/ctrl0000/ctrl0000gpu.h>
#include <ctrl/ctrl2080/ctrl2080nvd.h>
#include <nvdump.h>
const char* status_name(NvU32 s) {
 switch(s) {
#undef SDK_NVSTATUSCODES_H
#define NV_STATUS_CODE(name,code,str) case name: return #name;
#include <nvstatuscodes.h>
#undef NV_STATUS_CODE
 default:return "UNKNOWN_STATUS";
 }
}
template<class T> void call(int fd,unsigned nr,T& p) {
 if(ioctl(fd,_IOC(_IOC_READ|_IOC_WRITE,NV_IOCTL_MAGIC,nr,sizeof(T)),&p)<0)
  throw std::runtime_error(std::string("ioctl: ")+strerror(errno));
}
struct Client {
 int ctl=-1,gpu=-1;NvHandle root=0;
 ~Client(){if(root){NVOS00_PARAMETERS f{};f.hRoot=root;f.hObjectOld=root;
  if(ioctl(ctl,_IOC(_IOC_READ|_IOC_WRITE,NV_IOCTL_MAGIC,NV_ESC_RM_FREE,sizeof(f)),&f)<0 || f.status!=NV_OK)fprintf(stderr,"RM free failed\n");}
  if(gpu>=0){close(gpu);}
  if(ctl>=0){close(ctl);}}
 NvU32 control(NvHandle object,NvU32 cmd,void* p,NvU32 size){NVOS54_PARAMETERS a{};a.hClient=root;a.hObject=object;a.cmd=cmd;a.params=p;a.paramsSize=size;call(ctl,NV_ESC_RM_CONTROL,a);return a.status;}
 NvHandle alloc(NvHandle parent,NvU32 cls,void* p,NvU32 size,NvHandle requested){NVOS21_PARAMETERS a{};a.hRoot=root;a.hObjectParent=parent;a.hObjectNew=requested;a.hClass=cls;a.pAllocParms=p;a.paramsSize=size;call(ctl,NV_ESC_RM_ALLOC,a);fprintf(stderr,"alloc class=0x%x status=%s root=0x%x new=0x%x\n",cls,status_name(a.status),a.hRoot,a.hObjectNew);if(a.status!=NV_OK)throw std::runtime_error(status_name(a.status));return a.hObjectNew;}
};
int main(int argc,char** argv){try{
 if(argc!=2)throw std::runtime_error("Usage: read_nvidia_vbios_dump OUTPUT (new file) | --probe-rom-api");
 std::ifstream version_file("/sys/module/nvidia/version");std::string running_version;std::getline(version_file,running_version);if(running_version!="615.78.08")throw std::runtime_error("Running NVIDIA version differs from pinned 615.78.08 headers");
 Client c;c.ctl=open("/dev/nvidiactl",O_RDWR|O_CLOEXEC);if(c.ctl<0)throw std::runtime_error(strerror(errno));
 nv_ioctl_card_info_t cards[32]{};call(c.ctl,NV_ESC_CARD_INFO,cards);nv_ioctl_card_info_t* card=nullptr;
 for(auto& x:cards)if(x.valid){if(card)throw std::runtime_error("Expected one GPU; explicit selection required");card=&x;}
 if(!card)throw std::runtime_error("No valid GPU card info");
 char path[64];snprintf(path,sizeof(path),"/dev/nvidia%u",card->minor_number);c.gpu=open(path,O_RDWR|O_CLOEXEC);if(c.gpu<0)throw std::runtime_error(strerror(errno));nv_ioctl_register_fd_t reg{};reg.ctl_fd=c.ctl;call(c.gpu,NV_ESC_REGISTER_FD,reg);
 c.root=c.alloc(0,NV01_ROOT_CLIENT,nullptr,0,0);
 NV0000_CTRL_GPU_GET_ID_INFO_V2_PARAMS info{};info.gpuId=card->gpu_id;auto rc=c.control(c.root,NV0000_CTRL_CMD_GPU_GET_ID_INFO_V2,&info,sizeof(info));if(rc!=NV_OK)throw std::runtime_error(status_name(rc));
 NV0080_ALLOC_PARAMETERS dp{};dp.deviceId=info.deviceInstance;auto dev=c.alloc(c.root,NV01_DEVICE_0,&dp,sizeof(dp),0x1001);
 NV2080_ALLOC_PARAMETERS sp{};sp.subDeviceId=info.subDeviceInstance;auto sub=c.alloc(dev,NV20_SUBDEVICE_0,&sp,sizeof(sp),0x1002);

 // Offline-discovered command 0x20800801, GSP handler 0x1420110.
 // Probe only: zero size is rejected before the handler dereferences pBuffer.
 // No guessed pointer serialization or full-ROM request is attempted here.
 if(std::string(argv[1])=="--probe-rom-api") {
  struct { NvU32 size; NvU32 padding; NvU64 pBuffer; } probe{};
  static_assert(sizeof(probe)==16);
  rc=c.control(sub,0x20800801,&probe,sizeof(probe));
  printf("rom_api_probe command=0x20800801 status=%s size=%u buffer=0x%llx\n",
         status_name(rc),probe.size,(unsigned long long)probe.pBuffer);
  return 0;
 }

 NV2080_CTRL_NVD_GET_DUMP_SIZE_PARAMS query{};
 query.component=NVDUMP_COMPONENT_ENG_VBIOS;
 rc=c.control(sub,NV2080_CTRL_CMD_NVD_GET_DUMP_SIZE,&query,sizeof(query));
 printf("component=%u status=%s bytes=%u\n",query.component,status_name(rc),query.size);
 if(rc!=NV_OK || query.size==0 || query.size>1024*1024)throw std::runtime_error("Invalid dump size or failed query");
 // The size is only an estimate. The live driver rejected a buffer of exactly
 // its reported 250 bytes. Reserve a bounded 1 MiB; do not retry automatically.
 std::vector<NvU8> buffer(1024*1024);
 NV2080_CTRL_NVD_GET_DUMP_PARAMS dump{};
 dump.component=query.component;dump.size=buffer.size();dump.pBuffer=buffer.data();
 rc=c.control(sub,NV2080_CTRL_CMD_NVD_GET_DUMP,&dump,sizeof(dump));
 printf("dump component=%u status=%s bytes=%u\n",dump.component,status_name(rc),dump.size);
 if(rc!=NV_OK || dump.size>buffer.size())throw std::runtime_error("Dump failed or returned an invalid extent");
 if(!dump.size)throw std::runtime_error("Empty component; no output written");
 int out=open(argv[1],O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC,0644);
 if(out<0)throw std::runtime_error(strerror(errno));
 size_t pos=0;
 while(pos<dump.size){ssize_t n=write(out,buffer.data()+pos,dump.size-pos);
  if(n<0 && errno==EINTR)continue;
  if(n<=0){close(out);throw std::runtime_error("Output write failed; partial file may remain");}
  pos+=n;}
 if(close(out))throw std::runtime_error("Output close failed");
 return 0;
 }catch(const std::exception& e){fprintf(stderr,"%s\n",e.what());return 1;}}
