// SPDX-License-Identifier: MIT
// Public cuDLA API regressions: real CUDA, explicitly modeled NPU transport.
#include <cudla.h>
#include <cuda.h>
#include <cassert>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <iterator>
#include <vector>

int main(int argc,char **argv) {
    assert(argc==2 || argc==3);
    std::ifstream file(argv[1],std::ios::binary);
    std::vector<uint8_t> bytes((std::istreambuf_iterator<char>(file)),{});
    assert(!bytes.empty());
    assert(cuInit(0)==CUDA_SUCCESS);
    CUcontext ctx=nullptr; assert(cuDevicePrimaryCtxRetain(&ctx,0)==CUDA_SUCCESS);
    assert(cuCtxSetCurrent(ctx)==CUDA_SUCCESS);
    CUstream stream=nullptr; assert(cuStreamCreate(&stream,CU_STREAM_NON_BLOCKING)==CUDA_SUCCESS);
    cudlaDevHandle a=nullptr,b=nullptr,unchanged=reinterpret_cast<cudlaDevHandle>(0x123);
    assert(cudlaCreateDevice(0,&unchanged,CUDLA_STANDALONE)==cudlaErrorUnsupportedOperation);
    assert(unchanged==reinterpret_cast<cudlaDevHandle>(0x123));
    assert(cudlaCreateDevice(0,&a,0)==cudlaSuccess && cudlaCreateDevice(0,&b,0)==cudlaSuccess);
    assert(a!=b);
    cudlaModule model=nullptr;
    assert(cudlaModuleLoadFromMemory(a,bytes.data(),7,&model,0)==cudlaErrorInvalidModule && !model);
    assert(cudlaModuleLoadFromMemory(a,bytes.data(),bytes.size(),&model,0)==cudlaSuccess);
    cudlaModuleAttribute attr{};
    cudlaModuleTensorDescriptor in{},out{};
    attr.inputTensorDesc=&in; assert(cudlaModuleGetAttributes(model,CUDLA_INPUT_TENSOR_DESCRIPTORS,&attr)==cudlaSuccess);
    attr.outputTensorDesc=&out; assert(cudlaModuleGetAttributes(model,CUDLA_OUTPUT_TENSOR_DESCRIPTORS,&attr)==cudlaSuccess);
    CUdeviceptr input=0,output=0; assert(cuMemAlloc(&input,in.size)==CUDA_SUCCESS);
    assert(cuMemAlloc(&output,out.size)==CUDA_SUCCESS);
    uint64_t *ih=nullptr,*oh=nullptr,*extra=nullptr;
    assert(cudlaMemRegister(a,reinterpret_cast<uint64_t*>(input+1),in.size-1,&extra,0)==cudlaErrorInvalidAddress);
    assert(cudlaMemRegister(a,reinterpret_cast<uint64_t*>(input),in.size+1,&extra,0)==cudlaErrorInvalidAddress);
    assert(cudlaMemRegister(a,reinterpret_cast<uint64_t*>(input),in.size,&ih,0)==cudlaSuccess);
    assert(cudlaMemRegister(a,reinterpret_cast<uint64_t*>(input),in.size,&extra,0)==cudlaErrorMemoryRegistered);
    assert(cudlaMemRegister(a,reinterpret_cast<uint64_t*>(output),out.size,&oh,0)==cudlaSuccess);
    assert(cudlaMemUnregister(b,ih)==cudlaErrorInvalidAddress);
    const uint64_t *inputs[]={ih}; uint64_t *outputs[]={oh};
    cudlaTask task{};task.moduleHandle=model;task.inputTensor=inputs;task.outputTensor=outputs;
    task.numInputTensors=task.numOutputTensors=1;
    assert(cudlaSubmitTask(b,&task,1,stream,0)==cudlaErrorInvalidModule);
    assert(cudlaSubmitTask(a,&task,2,stream,0)==cudlaErrorUnsupportedOperation);
    assert(cudlaSubmitTask(a,&task,1,stream,1)==cudlaErrorUnsupportedOperation);
    task.numInputTensors=0;assert(cudlaSubmitTask(a,&task,1,stream,0)==cudlaErrorInvalidParam);task.numInputTensors=1;
    assert(cuStreamBeginCapture(stream,CU_STREAM_CAPTURE_MODE_THREAD_LOCAL)==CUDA_SUCCESS);
    assert(cudlaSubmitTask(a,&task,1,stream,0)==cudlaErrorUnsupportedOperation);
    CUgraph graph=nullptr;assert(cuStreamEndCapture(stream,&graph)==CUDA_SUCCESS);
    assert(cuGraphDestroy(graph)==CUDA_SUCCESS);
    // Registration tokens cannot keep an allocation alive after cudaFree.
    assert(cuMemFree(input)==CUDA_SUCCESS);input=0;
    assert(cudlaSubmitTask(a,&task,1,stream,0)==cudlaErrorInvalidAddress);
    assert(cudlaMemUnregister(a,ih)==cudlaSuccess);
    assert(cudlaMemUnregister(a,ih)==cudlaErrorInvalidAddress);
    assert(cuMemAlloc(&input,in.size)==CUDA_SUCCESS);
    assert(cudlaMemRegister(a,reinterpret_cast<uint64_t*>(input),in.size,&extra,0)==cudlaSuccess);
    assert(extra!=ih);inputs[0]=extra;
    assert(cuMemsetD8(input,0x46,in.size)==CUDA_SUCCESS);assert(cuCtxSynchronize()==CUDA_SUCCESS);
    assert(cudlaGetLastError(a)==cudlaSuccess);
    if(argc==3) for(unsigned int i=0;i<64;i++)
        assert(cudlaSubmitTask(a,&task,1,stream,0)==cudlaSuccess);
    assert(!setenv("TEST_FAILURE","exec",1));
    assert(cudlaSubmitTask(a,&task,1,stream,0)==cudlaErrorDlaErrEngineTimeout);
    assert(cudlaGetLastError(a)==cudlaErrorDlaErrEngineTimeout);
    assert(!setenv("TEST_FAILURE","",1));
    assert(cudlaSubmitTask(a,&task,1,stream,0)==cudlaErrorDlaErrEngineTimeout); // no retry
    assert(cudlaMemUnregister(a,extra)==cudlaSuccess && cudlaMemUnregister(a,oh)==cudlaSuccess);
    assert(cudlaModuleUnload(model,0)==cudlaSuccess);
    assert(cudlaModuleUnload(model,0)==cudlaErrorInvalidModule);
    assert(cudlaDestroyDevice(a)==cudlaSuccess && cudlaDestroyDevice(b)==cudlaSuccess);
    assert(cudlaDestroyDevice(a)==cudlaErrorInvalidDevice);
    assert(cuStreamDestroy(stream)==CUDA_SUCCESS);
    assert(cuMemFree(input)==CUDA_SUCCESS && cuMemFree(output)==CUDA_SUCCESS);
    assert(cuDevicePrimaryCtxRelease(0)==CUDA_SUCCESS);
    std::cout<<"public API ownership, capture, invalid/freed memory and sticky errors passed\n";
}
