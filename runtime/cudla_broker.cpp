// SPDX-License-Identifier: MIT
/* Minimal privileged cuDLA broker.
 *
 * Models and libraries are root-owned installed artifacts. Tensor bytes travel
 * only over stdin/stdout; this process never parses images or writes paths
 * supplied by its caller. Any protocol or device error terminates the process
 * without retrying an uncertain submission.
 */
#include <cudla.h>
#include <cuda.h>
#include <openssl/evp.h>
#include <linux/capability.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <grp.h>
#include <poll.h>
#include <pwd.h>
#include <signal.h>
#include <unistd.h>
#include <fcntl.h>
#include <cerrno>
#include <array>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>
#include "n1x_dla.h"
#include "n1x_cudla_private.h"

namespace {
constexpr char model_directory[]="/usr/lib/n1x-npu-linux/models";
constexpr uint16_t protocol_version=1, info_command=0xfffe, quit_command=0xffff;
constexpr size_t max_model_bytes=size_t{64}*1024U*1024U;
constexpr size_t max_tensor_bytes=size_t{32}*1024U*1024U;
constexpr size_t max_total_model_bytes=size_t{256}*1024U*1024U;
constexpr size_t max_total_tensor_bytes=size_t{256}*1024U*1024U;
constexpr size_t max_retained_bytes=size_t{64}*1024U*1024U;
constexpr size_t job_prefix_bytes=8192, page_bytes=4096;
constexpr int protocol_timeout_ms=60000;

struct Header { char magic[4]; uint16_t version,code; uint64_t bytes; };
struct ModelInfo { uint64_t input_bytes,output_bytes; uint8_t sha256[32]; };
struct Identity { uid_t uid; gid_t gid; };
static_assert(sizeof(Header)==16 && sizeof(ModelInfo)==48 && sizeof(n1x_dla_accel_info)==48);

void cuda_check(CUresult status) {
    if(status!=CUDA_SUCCESS) throw std::runtime_error("CUDA status "+std::to_string(status));
}
void dla_check(cudlaStatus status) {
    if(status!=cudlaSuccess) throw std::runtime_error("cuDLA status "+std::to_string(status));
}
void exact_file_read(int fd,void *buffer,size_t bytes) {
    auto *p=static_cast<uint8_t*>(buffer);
    while(bytes) {
        auto n=::read(fd,p,bytes);
        if(n<0 && errno==EINTR) continue;
        if(n<=0) throw std::runtime_error("truncated broker request");
        p+=n; bytes-=static_cast<size_t>(n);
    }
}
void wait_ready(int fd,short events) {
    pollfd descriptor{fd,events,0};
    for(;;) {
        int status=poll(&descriptor,1,protocol_timeout_ms);
        if(status<0 && errno==EINTR) continue;
        if(!status) throw std::runtime_error("broker protocol timeout");
        auto returned=static_cast<unsigned int>(static_cast<unsigned short>(descriptor.revents));
        auto errors=static_cast<unsigned int>(POLLERR)|static_cast<unsigned int>(POLLNVAL);
        auto ready=static_cast<unsigned int>(static_cast<unsigned short>(events))|static_cast<unsigned int>(POLLHUP);
        if(status<0 || (returned&errors))
            throw std::runtime_error("broker protocol poll failed");
        if(returned&ready) return;
    }
}
void exact_protocol_read(void *buffer,size_t bytes) {
    auto *p=static_cast<uint8_t*>(buffer);
    while(bytes) {
        wait_ready(STDIN_FILENO,POLLIN);
        auto n=::read(STDIN_FILENO,p,bytes);
        if(n<0 && errno==EINTR) continue;
        if(n<=0) throw std::runtime_error("truncated broker request");
        p+=n;bytes-=static_cast<size_t>(n);
    }
}
bool read_header(Header& header) {
    size_t done=0;
    while(done<sizeof(header)) {
        wait_ready(STDIN_FILENO,POLLIN);
        auto n=::read(STDIN_FILENO,reinterpret_cast<uint8_t*>(&header)+done,sizeof(header)-done);
        if(n<0 && errno==EINTR) continue;
        if(!n && !done) return false;
        if(n<=0) throw std::runtime_error("truncated broker header");
        done+=static_cast<size_t>(n);
    }
    return true;
}
void exact_write(const void *buffer,size_t bytes) {
    auto *p=static_cast<const uint8_t*>(buffer);
    while(bytes) {
        wait_ready(STDOUT_FILENO,POLLOUT);
        auto n=::write(STDOUT_FILENO,p,bytes);
        if(n<0 && errno==EINTR) continue;
        if(n<=0) throw std::runtime_error("broker response write failed");
        p+=n; bytes-=static_cast<size_t>(n);
    }
}
void response(uint16_t code,const void *payload,size_t bytes) {
    Header header{{'N','1','X','R'},protocol_version,code,bytes};
    exact_write(&header,sizeof(header));
    if(bytes) exact_write(payload,bytes);
}
bool valid_id(const std::string& id) {
    if(id.empty() || id.size()>64) return false;
    for(unsigned char c:id)
        if(!(c>='a'&&c<='z') && !(c>='0'&&c<='9') && c!='-') return false;
    return true;
}
void secure_stat(const struct stat& st,const char *what) {
    if(st.st_uid || (st.st_mode&static_cast<mode_t>(0022)))
        throw std::runtime_error(std::string(what)+" is not root-owned and immutable");
}
std::vector<uint8_t> read_model(int directory,const std::string& id,ModelInfo& info) {
    if(!valid_id(id)) throw std::runtime_error("invalid model identifier");
    std::string name=id+".nvda";
    int fd=openat(directory,name.c_str(),O_RDONLY|O_CLOEXEC|O_NOFOLLOW);
    if(fd<0) throw std::runtime_error("installed model is absent");
    struct stat st{};
    if(fstat(fd,&st) || !S_ISREG(st.st_mode) || st.st_size<8 ||
       static_cast<uint64_t>(st.st_size)>max_model_bytes) {
        close(fd); throw std::runtime_error("invalid installed model");
    }
    secure_stat(st,"installed model");
    std::vector<uint8_t> data(static_cast<size_t>(st.st_size));
    try { exact_file_read(fd,data.data(),data.size()); } catch(...) { close(fd); throw; }
    close(fd);
    unsigned int length=0;
    EVP_MD_CTX *context=EVP_MD_CTX_new();
    if(!context || EVP_DigestInit_ex(context,EVP_sha256(),nullptr)!=1 ||
       EVP_DigestUpdate(context,data.data(),data.size())!=1 ||
       EVP_DigestFinal_ex(context,info.sha256,&length)!=1 || length!=sizeof(info.sha256)) {
        EVP_MD_CTX_free(context); throw std::runtime_error("model digest failed");
    }
    EVP_MD_CTX_free(context);
    return data;
}

struct Module {
    cudlaDevHandle device{}; cudlaModule handle{};
    std::vector<CUdeviceptr> inputs{},outputs{};
    std::vector<uint64_t*> input_handles{},output_handles{};
    std::vector<cudlaModuleTensorDescriptor> ins{},outs{};
    uint64_t total_input_bytes{},total_output_bytes{};
    Module(cudlaDevHandle d,std::vector<uint8_t>& model,size_t tensor_bytes):device(d) {
        try {
            dla_check(cudlaModuleLoadFromMemory(device,model.data(),model.size(),&handle,0));
            cudlaModuleAttribute attribute{};
            dla_check(cudlaModuleGetAttributes(handle,CUDLA_NUM_INPUT_TENSORS,&attribute));
            if(attribute.numInputTensors<1 || attribute.numInputTensors>64)
                throw std::runtime_error("broker requires 1..64 model inputs");
            ins.resize(attribute.numInputTensors);
            dla_check(cudlaModuleGetAttributes(handle,CUDLA_NUM_OUTPUT_TENSORS,&attribute));
            if(attribute.numOutputTensors<1 || attribute.numOutputTensors>64)
                throw std::runtime_error("broker requires 1..64 model outputs");
            outs.resize(attribute.numOutputTensors);
            attribute.inputTensorDesc=ins.data();
            dla_check(cudlaModuleGetAttributes(handle,CUDLA_INPUT_TENSOR_DESCRIPTORS,&attribute));
            attribute.outputTensorDesc=outs.data();
            dla_check(cudlaModuleGetAttributes(handle,CUDLA_OUTPUT_TENSOR_DESCRIPTORS,&attribute));
            for(const auto& d_in:ins) {
                if(!d_in.size || d_in.size>max_tensor_bytes ||
                   d_in.size>max_total_tensor_bytes-total_input_bytes)
                    throw std::runtime_error("invalid broker tensor size");
                total_input_bytes+=d_in.size;
            }
            for(const auto& d_out:outs) {
                if(!d_out.size || d_out.size>max_tensor_bytes ||
                   d_out.size>max_total_tensor_bytes-total_output_bytes ||
                   total_input_bytes>max_total_tensor_bytes-total_output_bytes)
                    throw std::runtime_error("invalid broker tensor size");
                total_output_bytes+=d_out.size;
            }
            if(tensor_bytes>max_total_tensor_bytes-static_cast<size_t>(total_input_bytes+total_output_bytes))
                throw std::runtime_error("invalid broker tensor size");
            inputs.resize(ins.size(),0); input_handles.resize(ins.size(),nullptr);
            outputs.resize(outs.size(),0); output_handles.resize(outs.size(),nullptr);
            for(size_t i=0;i<ins.size();i++) {
                cuda_check(cuMemAlloc(&inputs[i],ins[i].size));
                dla_check(cudlaMemRegister(device,reinterpret_cast<uint64_t*>(inputs[i]),ins[i].size,&input_handles[i],0));
            }
            for(size_t i=0;i<outs.size();i++) {
                cuda_check(cuMemAlloc(&outputs[i],outs[i].size));
                dla_check(cudlaMemRegister(device,reinterpret_cast<uint64_t*>(outputs[i]),outs[i].size,&output_handles[i],0));
            }
        } catch(...) {
            cleanup();
            throw;
        }
    }
    void cleanup() noexcept {
        for(auto *h:input_handles) if(h) cudlaMemUnregister(device,h);
        for(auto *h:output_handles) if(h) cudlaMemUnregister(device,h);
        input_handles.clear(); output_handles.clear();
        if(handle) { cudlaModuleUnload(handle,0); handle=nullptr; }
        for(auto p:inputs) if(p) cuMemFree(p);
        for(auto p:outputs) if(p) cuMemFree(p);
        inputs.clear(); outputs.clear();
    }
    Module(const Module&)=delete; Module& operator=(const Module&)=delete;
    Module(Module&& other) noexcept:
        device(other.device),handle(other.handle),inputs(std::move(other.inputs)),outputs(std::move(other.outputs)),
        input_handles(std::move(other.input_handles)),output_handles(std::move(other.output_handles)),
        ins(std::move(other.ins)),outs(std::move(other.outs)),
        total_input_bytes(other.total_input_bytes),total_output_bytes(other.total_output_bytes) {
        other.handle=nullptr;
    }
    Module& operator=(Module&&)=delete;
    ~Module() { cleanup(); }
    std::vector<uint8_t> run(const std::vector<uint8_t>& data,CUstream stream) {
        if(data.size()!=total_input_bytes) throw std::runtime_error("broker input size mismatch");
        size_t in_cursor=0;
        for(size_t i=0;i<ins.size();i++) {
            cuda_check(cuMemcpyHtoD(inputs[i],data.data()+in_cursor,ins[i].size));
            in_cursor+=ins[i].size;
        }
        for(size_t i=0;i<outs.size();i++) {
            cuda_check(cuMemsetD8(outputs[i],0xa5,outs[i].size));
        }
        cuda_check(cuCtxSynchronize());
        std::vector<const uint64_t*> in_ptrs(input_handles.begin(),input_handles.end());
        cudlaTask task{};task.moduleHandle=handle;
        task.inputTensor=in_ptrs.data();task.outputTensor=output_handles.data();
        task.numInputTensors=static_cast<uint32_t>(ins.size());
        task.numOutputTensors=static_cast<uint32_t>(outs.size());
        dla_check(cudlaSubmitTask(device,&task,1,stream,0));
        cuda_check(cuStreamSynchronize(stream)); dla_check(cudlaGetLastError(device));
        std::vector<uint8_t> result(total_output_bytes);
        size_t out_cursor=0;
        for(size_t i=0;i<outs.size();i++) {
            cuda_check(cuMemcpyDtoH(result.data()+out_cursor,outputs[i],outs[i].size));
            out_cursor+=outs[i].size;
        }
        return result;
    }
};

void bind_to_parent(pid_t parent) {
    int signal=0;
    if(prctl(PR_SET_PDEATHSIG,SIGTERM) || prctl(PR_GET_PDEATHSIG,&signal) ||
       signal!=SIGTERM || getppid()!=parent)
        throw std::runtime_error("cannot bind broker lifetime to its caller");
}
Identity broker_identity() {
    passwd record{},*found=nullptr;std::array<char,16384> buffer{};
    int error=getpwnam_r("n1x-dla",&record,buffer.data(),buffer.size(),&found);
    if(error || !found || !record.pw_uid || !record.pw_gid)
        throw std::runtime_error("dedicated n1x-dla account is absent or unsafe");
    return {record.pw_uid,record.pw_gid};
}
void drop_privileges(pid_t parent,Identity identity) {
    if(setgroups(0,nullptr) || prctl(PR_SET_KEEPCAPS,1) || setgid(identity.gid) || setuid(identity.uid))
        throw std::runtime_error("privilege drop failed");
    __user_cap_header_struct header{_LINUX_CAPABILITY_VERSION_3,0};
    __user_cap_data_struct data[2]{};
    constexpr unsigned rawio=static_cast<unsigned>(CAP_SYS_RAWIO);
    data[rawio/32U].effective=UINT32_C(1)<<(rawio%32U);
    data[rawio/32U].permitted=data[rawio/32U].effective;
    if(syscall(SYS_capset,&header,&data) || prctl(PR_SET_KEEPCAPS,0) ||
       prctl(PR_SET_NO_NEW_PRIVS,1,0,0,0) || prctl(PR_SET_DUMPABLE,0))
        throw std::runtime_error("capability confinement failed");
    /* setgid/setuid clear PDEATHSIG. Re-arm it after the credential changes,
     * then close the race by checking the original parent again.
     */
    bind_to_parent(parent);
}
}

int main(int argc,char **argv) {
    cudlaDevHandle device=nullptr;CUdevice gpu=0;CUcontext context=nullptr;CUstream stream=nullptr;
    int directory=-1,info_fd=-1,exit_code=1;
    std::vector<Module> modules;
    try {
        if(geteuid()!=0 || argc<2 || argc>65) throw std::runtime_error("broker requires pkexec and 1..64 models");
        pid_t parent=getppid();
        Identity identity=broker_identity();
        bind_to_parent(parent);
        if(signal(SIGPIPE,SIG_IGN)==SIG_ERR) throw std::runtime_error("cannot disable SIGPIPE");
        umask(077); if(chdir("/")) throw std::runtime_error("cannot enter safe working directory");
        directory=open(model_directory,O_RDONLY|O_DIRECTORY|O_CLOEXEC|O_NOFOLLOW);
        struct stat directory_stat{};
        if(directory<0 || fstat(directory,&directory_stat) || !S_ISDIR(directory_stat.st_mode))
            throw std::runtime_error("secure model directory is absent");
        secure_stat(directory_stat,"model directory");
        std::vector<std::vector<uint8_t>> images;std::vector<ModelInfo> model_info(argc-1);
        std::set<std::string> identifiers;size_t model_bytes=0;
        for(int i=1;i<argc;i++) {
            std::string identifier=argv[i];
            if(!identifiers.insert(identifier).second) throw std::runtime_error("duplicate model identifier");
            auto image=read_model(directory,identifier,model_info[i-1]);
            if(image.size()>max_total_model_bytes-model_bytes)
                throw std::runtime_error("aggregate model size exceeds broker limit");
            model_bytes+=image.size();images.push_back(std::move(image));
        }
        close(directory);directory=-1;
        cuda_check(cuInit(0));cuda_check(cuDeviceGet(&gpu,0));cuda_check(cuDevicePrimaryCtxRetain(&context,gpu));
        cuda_check(cuCtxSetCurrent(context));cuda_check(cuStreamCreate(&stream,CU_STREAM_NON_BLOCKING));
        uint64_t count=0;dla_check(cudlaDeviceGetCount(&count));
        if(count!=1) throw std::runtime_error("expected exactly one N1X DLA");
        dla_check(cudlaCreateDevice(0,&device,CUDLA_CUDA_DLA));modules.reserve(images.size());
        size_t tensor_bytes=0,retained_bytes=0;
        for(size_t i=0;i<images.size();i++) {
            modules.emplace_back(device,images[i],tensor_bytes);
            tensor_bytes+=static_cast<size_t>(modules.back().total_input_bytes+modules.back().total_output_bytes);
            model_info[i].input_bytes=modules.back().total_input_bytes;
            model_info[i].output_bytes=modules.back().total_output_bytes;
            uint64_t image_bytes=0;
            if(n1x_cudla_materialized_image_size(images[i].data(),images[i].size(),&image_bytes) ||
               image_bytes>SIZE_MAX-job_prefix_bytes-(page_bytes-1))
                throw std::runtime_error("cannot determine retained model size");
            size_t required=(static_cast<size_t>(image_bytes)+job_prefix_bytes+page_bytes-1)&~(page_bytes-1);
            if(required>max_retained_bytes-retained_bytes)
                throw std::runtime_error("requested models exceed retained-DMA limit");
            retained_bytes+=required;
        }
        images.clear();images.shrink_to_fit();
        info_fd=n1x_dla_open("/dev/accel/accel0");if(info_fd<0) throw std::runtime_error("accel info open failed");
        n1x_dla_accel_info capacity{};int info_error=n1x_dla_info(info_fd,&capacity);
        if(info_error) throw std::runtime_error("accel capacity query failed "+std::to_string(info_error));
        if(capacity.retained_bytes>capacity.retain_limit || retained_bytes>capacity.retain_limit-capacity.retained_bytes ||
           capacity.retained_jobs>capacity.max_jobs || modules.size()>capacity.max_jobs-capacity.retained_jobs)
            throw std::runtime_error("insufficient retained-DMA budget for complete broker session");
        drop_privileges(parent,identity);
        Header hello{{'N','1','X','B'},protocol_version,static_cast<uint16_t>(modules.size()),model_info.size()*sizeof(ModelInfo)};
        exact_write(&hello,sizeof(hello));exact_write(model_info.data(),model_info.size()*sizeof(ModelInfo));
        Header request{};
        while(read_header(request)) {
            if(std::memcmp(request.magic,"N1XQ",4)!=0 || request.version!=protocol_version)
                throw std::runtime_error("invalid broker protocol");
            if(request.code==quit_command) {
                if(request.bytes) throw std::runtime_error("invalid quit request");
                exit_code=0;break;
            }
            if(request.code==info_command) {
                if(request.bytes) throw std::runtime_error("invalid info request");
                n1x_dla_accel_info info{};int err=n1x_dla_info(info_fd,&info);
                if(err) throw std::runtime_error("accel info failed "+std::to_string(err));
                response(info_command,&info,sizeof(info));continue;
            }
            if(request.code>=modules.size() || request.bytes!=modules[request.code].total_input_bytes)
                throw std::runtime_error("invalid inference request");
            std::vector<uint8_t> input(request.bytes);exact_protocol_read(input.data(),input.size());
            auto output=modules[request.code].run(input,stream);response(request.code,output.data(),output.size());
        }
    } catch(const std::exception& error) {
        std::cerr<<"n1x-dla-broker: "<<error.what()<<"; stopped without retry\n";
    }
    if(info_fd>=0) close(info_fd);
    modules.clear();
    if(device) cudlaDestroyDevice(device);
    if(stream) cuStreamDestroy(stream);
    if(context) cuDevicePrimaryCtxRelease(gpu);
    if(directory>=0) close(directory);
    return exit_code;
}
