// SPDX-License-Identifier: MIT
// A real cuDLA application. Links NVIDIA libcudla, never our backend directly.
// Reference output is supplied independently; this program performs no inference.
#include <cudla.h>
#include <cuda.h>
#include <cstdint>
#include <cstring>
#include <fcntl.h>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <unistd.h>
#include <utility>
#include <vector>

static void cuda_check(CUresult status) {
    if(status!=CUDA_SUCCESS) throw std::runtime_error("CUDA status "+std::to_string(status));
}
static void dla_check(cudlaStatus status) {
    if(status!=cudlaSuccess) throw std::runtime_error("cuDLA status "+std::to_string(status));
}
static std::vector<uint8_t> read(const char *path,size_t maximum) {
    std::ifstream file(path,std::ios::binary|std::ios::ate);
    if(!file || file.tellg()<=0 || uint64_t(file.tellg())>maximum) throw std::runtime_error("invalid input file");
    std::vector<uint8_t> data(static_cast<size_t>(file.tellg()));
    file.seekg(0); file.read(reinterpret_cast<char*>(data.data()),data.size());
    if(!file) throw std::runtime_error("input read failed");
    return data;
}
int main(int argc,char **argv) {
    if(argc<5 || (argc-2)%3 || argc>197) {
        std::cerr<<"usage: cudla-smoke MODEL.nvda INPUT.fp16 EXPECTED.fp16 NEW-OUTPUT.fp16 [INPUT EXPECTED NEW-OUTPUT ...]\n";
        return 2;
    }
    cudlaDevHandle device=nullptr; cudlaModule module=nullptr;
    CUdevice gpu=0; CUcontext context=nullptr; CUstream stream=nullptr;
    CUdeviceptr input=0,output=0; uint64_t *input_handle=nullptr,*output_handle=nullptr;
    struct Probe { std::vector<uint8_t> input, expected; int fd=-1; };
    std::vector<Probe> probes;
    int exit_code=1;
    try {
        auto model=read(argv[1],64U<<20);
        probes.reserve((argc-2)/3);
        // Validate every input and reserve all outputs before the first submission.
        for(int i=2;i<argc;i+=3) {
            Probe probe{read(argv[i],32U<<20),read(argv[i+1],32U<<20)};
            if(!probes.empty() && (probe.input.size()!=probes[0].input.size() ||
                                  probe.expected.size()!=probes[0].expected.size()))
                throw std::runtime_error("tensor byte count changes between probes");
            probe.fd=open(argv[i+2],O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC,0600);
            if(probe.fd<0) throw std::runtime_error("output must be a new writable file");
            probes.push_back(std::move(probe));
        }
        cuda_check(cuInit(0)); cuda_check(cuDeviceGet(&gpu,0));
        cuda_check(cuDevicePrimaryCtxRetain(&context,gpu)); cuda_check(cuCtxSetCurrent(context));
        cuda_check(cuStreamCreate(&stream,CU_STREAM_NON_BLOCKING));
        uint64_t version=0,count=0;
        dla_check(cudlaGetVersion(&version)); dla_check(cudlaDeviceGetCount(&count));
        if(count!=1) throw std::runtime_error("expected exactly one N1X DLA");
        dla_check(cudlaCreateDevice(0,&device,CUDLA_CUDA_DLA));
        dla_check(cudlaModuleLoadFromMemory(device,model.data(),model.size(),&module,0));
        cudlaModuleAttribute attribute{};
        dla_check(cudlaModuleGetAttributes(module,CUDLA_NUM_INPUT_TENSORS,&attribute));
        if(attribute.numInputTensors!=1) throw std::runtime_error("probe requires one input");
        dla_check(cudlaModuleGetAttributes(module,CUDLA_NUM_OUTPUT_TENSORS,&attribute));
        if(attribute.numOutputTensors!=1) throw std::runtime_error("probe requires one output");
        cudlaModuleTensorDescriptor in{},out{};
        attribute.inputTensorDesc=&in; dla_check(cudlaModuleGetAttributes(module,CUDLA_INPUT_TENSOR_DESCRIPTORS,&attribute));
        attribute.outputTensorDesc=&out; dla_check(cudlaModuleGetAttributes(module,CUDLA_OUTPUT_TENSOR_DESCRIPTORS,&attribute));
        if(in.size!=probes[0].input.size() || out.size!=probes[0].expected.size())
            throw std::runtime_error("tensor byte count mismatch");
        cuda_check(cuMemAlloc(&input,in.size)); cuda_check(cuMemAlloc(&output,out.size));
        dla_check(cudlaMemRegister(device,reinterpret_cast<uint64_t*>(input),in.size,&input_handle,0));
        dla_check(cudlaMemRegister(device,reinterpret_cast<uint64_t*>(output),out.size,&output_handle,0));
        const uint64_t *input_handles[]={input_handle}; uint64_t *output_handles[]={output_handle};
        cudlaTask task{}; task.moduleHandle=module; task.inputTensor=input_handles;
        task.outputTensor=output_handles; task.numInputTensors=task.numOutputTensors=1;
        for(auto& probe:probes) {
            const auto& data=probe.input;
            const auto& expected=probe.expected;
            cuda_check(cuMemcpyHtoD(input,data.data(),data.size()));
            cuda_check(cuMemsetD8(output,0xa5,expected.size()));
            cuda_check(cuCtxSynchronize());
            std::cout<<"cuDLA API "<<version<<", submitting "<<in.name<<" -> "<<out.name<<", no automatic retry"<<std::endl;
            dla_check(cudlaSubmitTask(device,&task,1,stream,0));
            cuda_check(cuStreamSynchronize(stream)); dla_check(cudlaGetLastError(device));
            std::vector<uint8_t> actual(expected.size()), unchanged(data.size());
            cuda_check(cuMemcpyDtoH(actual.data(),output,actual.size()));
            cuda_check(cuMemcpyDtoH(unchanged.data(),input,unchanged.size()));
            size_t done=0;
            while(done<actual.size()) {
                auto n=write(probe.fd,actual.data()+done,actual.size()-done);
                if(n<=0) throw std::runtime_error("output write failed");
                done+=n;
            }
            if(fsync(probe.fd)) throw std::runtime_error("output fsync failed");
            if(actual!=expected || unchanged!=data) throw std::runtime_error("numerical/input integrity mismatch");
            std::cout<<"PASS: "<<actual.size()<<" output bytes equal independent reference; input unchanged"<<std::endl;
        }
        exit_code=0;
    } catch(const std::exception& e) {
        std::cerr<<"FAIL: "<<e.what()<<"; stopped without retry"<<std::endl;
    }
    for(auto& probe:probes) if(probe.fd>=0) close(probe.fd);
    // Failed submissions can have retained kernel DMA. Destroying host-side
    // registrations does not reclaim those independent kernel allocations.
    if(input_handle) cudlaMemUnregister(device,input_handle);
    if(output_handle) cudlaMemUnregister(device,output_handle);
    if(module) cudlaModuleUnload(module,0);
    if(device) cudlaDestroyDevice(device);
    if(stream) cuStreamDestroy(stream);
    if(input) cuMemFree(input);
    if(output) cuMemFree(output);
    if(context) cuDevicePrimaryCtxRelease(gpu);
    return exit_code;
}
