// SPDX-License-Identifier: MIT
// Experimental N1X backend for NVIDIA's unmodified libcudla.so.1.
// Hybrid mode with explicit pinned-host staging and blocking submission.
// No CPU inference, implicit firmware startup, retry or fake NvSci objects.
#include "cudla_model.hpp"
#include "n1x_dla.h"
#include "n1x_cudla_private.h"
#include <cudlaExternalEtbl.hpp>
#include <cuda.h>
#include <cerrno>
#include <array>
#include <cstdlib>
#include <cstring>
#include <glob.h>
#include <map>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <unistd.h>

namespace {
struct Registration { CUdeviceptr pointer; size_t bytes; unsigned long long buffer_id; };
struct Device {
    int fd;
    CUcontext context;
    cudlaStatus last=cudlaSuccess;
    std::map<uintptr_t,Registration> memory;
    explicit Device(int f,CUcontext c):fd(f),context(c) {}
    ~Device() { if(fd>=0) close(fd); }
};
struct Module {
    uintptr_t owner;
    uint64_t retained_job=0;
    n1x::Model model;
    Module(uintptr_t d,const uint8_t *p,size_t n):owner(d),model(p,n) {}
};
std::mutex lock;
std::map<uintptr_t,std::unique_ptr<Device>> devices;
std::map<uintptr_t,std::unique_ptr<Module>> modules;
uintptr_t next_handle=0x10000;
// A failed CUDA synchronization is not proof that queued host DMA has ended.
// Keep uncertain staging allocations for process lifetime, bounded globally.
std::array<void*,128> quarantined{};
size_t quarantine_count=0, quarantine_bytes=0;
uintptr_t token() {
    if(next_handle>UINTPTR_MAX-32) throw std::bad_alloc();
    next_handle+=32; return next_handle;
}
template<class F> cudlaStatus boundary(F f) noexcept {
    try { std::lock_guard<std::mutex> guard(lock); return f(); }
    catch(const std::bad_alloc&) { return cudlaErrorOutOfResources; }
    catch(...) { return cudlaErrorUmd; }
}
Device *device(cudlaDevHandle h) {
    auto it=devices.find(reinterpret_cast<uintptr_t>(h));
    return it==devices.end()?nullptr:it->second.get();
}
Module *module(cudlaModule h) {
    auto it=modules.find(reinterpret_cast<uintptr_t>(h));
    return it==modules.end()?nullptr:it->second.get();
}
bool current(const Device& d) {
    CUcontext ctx=nullptr;
    return cuCtxGetCurrent(&ctx)==CUDA_SUCCESS && ctx && ctx==d.context;
}
int open_device() {
    if(const char *path=std::getenv("N1X_DLA_DEVICE")) return n1x_dla_open(path);
    glob_t paths{};
    if(glob("/dev/accel/accel*",0,nullptr,&paths)) { globfree(&paths); return -ENOENT; }
    int result=-ENOENT;
    for(size_t i=0;i<paths.gl_pathc;i++) {
        int fd=n1x_dla_open(paths.gl_pathv[i]);
        if(fd>=0) { result=fd; break; }
        if(fd!=-ENODEV) result=fd;
    }
    globfree(&paths); return result;
}
bool allocation(const Device& d,CUdeviceptr ptr,size_t bytes,unsigned long long *id) {
    CUmemorytype type; CUcontext ctx=nullptr; unsigned int managed=0;
    CUdeviceptr base=0; size_t capacity=0;
    if(!ptr || (ptr&31) || !bytes || bytes>N1X_DLA_MAX_IMAGE) return false;
    if(cuPointerGetAttribute(&type,CU_POINTER_ATTRIBUTE_MEMORY_TYPE,ptr)!=CUDA_SUCCESS || type!=CU_MEMORYTYPE_DEVICE ||
       cuPointerGetAttribute(&ctx,CU_POINTER_ATTRIBUTE_CONTEXT,ptr)!=CUDA_SUCCESS || ctx!=d.context ||
       cuPointerGetAttribute(&managed,CU_POINTER_ATTRIBUTE_IS_MANAGED,ptr)!=CUDA_SUCCESS || managed ||
       cuPointerGetAttribute(id,CU_POINTER_ATTRIBUTE_BUFFER_ID,ptr)!=CUDA_SUCCESS ||
       cuMemGetAddressRange(&base,&capacity,ptr)!=CUDA_SUCCESS) return false;
    return ptr>=base && ptr-base<capacity && bytes<=capacity-(ptr-base);
}
cudlaStatus kernel_error(int err) {
    if(err==-ENOMEM || err==-ENOSPC || err==-E2BIG) return cudlaErrorOutOfResources;
    if(err==-ETIMEDOUT) return cudlaErrorDlaErrEngineTimeout;
    if(err==-EBUSY) return cudlaErrorDlaErrProcessorBusy;
    return cudlaErrorUmd;
}
struct HostImage {
    void *data=nullptr;
    ~HostImage() { if(data) cuMemFreeHost(data); }
    void quarantine(size_t bytes) noexcept {
        quarantined[quarantine_count++]=data;
        quarantine_bytes+=bytes;
        data=nullptr;
    }
};
}

extern "C" {
int n1x_cudla_materialized_image_size(const void *loadable,size_t loadable_bytes,uint64_t *image_bytes) {
    if(!loadable || !loadable_bytes || !image_bytes) return -EINVAL;
    try {
        n1x::Model model(static_cast<const uint8_t*>(loadable),loadable_bytes);
        *image_bytes=model.image.size();
        return 0;
    } catch(const std::invalid_argument&) {
        return -EINVAL;
    } catch(const std::bad_alloc&) {
        return -ENOMEM;
    } catch(...) {
        return -EIO;
    }
}
cudlaStatus cudlaDrvGetVersion(uint64_t *version) {
    if(!version) return cudlaErrorInvalidParam;
    *version=1005000; return cudlaSuccess;
}
cudlaStatus cudlaDrvDeviceGetCount(uint64_t *count) {
    if(!count) return cudlaErrorInvalidParam;
    int fd=open_device();
    if(fd>=0) { close(fd); *count=1; return cudlaSuccess; }
    if(fd==-ENOENT || fd==-ENODEV) { *count=0; return cudlaSuccess; }
    return cudlaErrorOs;
}
cudlaStatus cudlaDrvCreateDevice(uint64_t index,cudlaDevHandle *out,uint32_t flags) {
    return boundary([&]() -> cudlaStatus {
        if(!out || index) return cudlaErrorInvalidParam;
        if(flags!=CUDLA_CUDA_DLA) return cudlaErrorUnsupportedOperation;
        if(devices.size()>=16) return cudlaErrorOutOfResources;
        if(cuInit(0)!=CUDA_SUCCESS) return cudlaErrorCuda;
        CUcontext ctx=nullptr;
        if(cuCtxGetCurrent(&ctx)!=CUDA_SUCCESS || !ctx) return cudlaErrorCuda;
        int fd=open_device(); if(fd<0) return cudlaErrorOs;
        // Keep the fd owned even if allocating the registry entry throws.
        std::unique_ptr<Device> d;
        try { d=std::make_unique<Device>(fd,ctx); } catch(...) { close(fd); throw; }
        auto key=token(); devices.emplace(key,std::move(d));
        *out=reinterpret_cast<cudlaDevHandle>(key); return cudlaSuccess;
    });
}
cudlaStatus cudlaDrvDestroyDevice(cudlaDevHandle handle) {
    return boundary([&]() -> cudlaStatus {
        if(!device(handle)) return cudlaErrorInvalidDevice;
        auto key=reinterpret_cast<uintptr_t>(handle);
        for(auto it=modules.begin();it!=modules.end();) {
            if(it->second->owner==key) it=modules.erase(it); else ++it;
        }
        // All backend submits are synchronous. Kernel-owned attempted DMA
        // remains retained independently of these host-side objects.
        devices.erase(key); return cudlaSuccess;
    });
}
cudlaStatus cudlaDrvDeviceGetAttribute(cudlaDevHandle handle,cudlaDevAttributeType type,cudlaDevAttribute *out) {
    return boundary([&]() -> cudlaStatus {
        if(!device(handle)) return cudlaErrorInvalidDevice;
        if(!out) return cudlaErrorInvalidParam;
        if(type!=CUDLA_UNIFIED_ADDRESSING) return cudlaErrorUnsupportedOperation;
        out->unifiedAddressingSupported=0; return cudlaSuccess;
    });
}
cudlaStatus cudlaDrvModuleLoadFromMemory(cudlaDevHandle handle,const uint8_t *bytes,size_t size,cudlaModule *out,uint32_t flags) {
    return boundary([&]() -> cudlaStatus {
        Device *d=device(handle); if(!d) return cudlaErrorInvalidDevice;
        if(d->last!=cudlaSuccess) return d->last;
        if(!bytes || !size || !out) return cudlaErrorInvalidParam;
        if(flags) return cudlaErrorUnsupportedOperation;
        size_t retained=0; for(const auto& m:modules) retained+=m.second->model.image.size();
        if(modules.size()>=64 || retained>=(256U<<20)) return cudlaErrorOutOfResources;
        std::unique_ptr<Module> m;
        try { m=std::make_unique<Module>(reinterpret_cast<uintptr_t>(handle),bytes,size); }
        catch(const std::invalid_argument&) { return cudlaErrorInvalidModule; }
        if(m->model.image.size()>(256U<<20)-retained) return cudlaErrorOutOfResources;
        auto key=token(); modules.emplace(key,std::move(m));
        *out=reinterpret_cast<cudlaModule>(key); return cudlaSuccess;
    });
}
cudlaStatus cudlaDrvModuleGetAttributes(cudlaModule handle,cudlaModuleAttributeType type,cudlaModuleAttribute *out) {
    return boundary([&]() -> cudlaStatus {
        Module *m=module(handle); if(!m) return cudlaErrorInvalidModule;
        if(!out) return cudlaErrorInvalidParam;
        switch(type) {
        case CUDLA_NUM_INPUT_TENSORS: out->numInputTensors=m->model.inputs.size(); break;
        case CUDLA_NUM_OUTPUT_TENSORS: out->numOutputTensors=m->model.outputs.size(); break;
        case CUDLA_NUM_OUTPUT_TASK_STATISTICS: out->numOutputTensors=0; break;
        case CUDLA_INPUT_TENSOR_DESCRIPTORS:
            if(!out->inputTensorDesc) return cudlaErrorInvalidParam;
            for(size_t i=0;i<m->model.inputs.size();i++) out->inputTensorDesc[i]=m->model.inputs[i].desc;
            break;
        case CUDLA_OUTPUT_TENSOR_DESCRIPTORS:
            if(!out->outputTensorDesc) return cudlaErrorInvalidParam;
            for(size_t i=0;i<m->model.outputs.size();i++) out->outputTensorDesc[i]=m->model.outputs[i].desc;
            break;
        default: return cudlaErrorUnsupportedOperation;
        }
        return cudlaSuccess;
    });
}
cudlaStatus cudlaDrvModuleUnload(cudlaModule handle,uint32_t flags) {
    return boundary([&]() -> cudlaStatus {
        if(!module(handle)) return cudlaErrorInvalidModule;
        if(flags) return cudlaErrorUnsupportedOperation;
        modules.erase(reinterpret_cast<uintptr_t>(handle)); return cudlaSuccess;
    });
}
cudlaStatus cudlaDrvMemRegister(cudlaDevHandle handle,const uint64_t *pointer,size_t size,uint64_t **out,uint32_t flags) {
    return boundary([&]() -> cudlaStatus {
        Device *d=device(handle); if(!d) return cudlaErrorInvalidDevice;
        if(d->last!=cudlaSuccess) return d->last;
        if(!out) return cudlaErrorInvalidParam;
        if(flags) return cudlaErrorUnsupportedOperation;
        if(!current(*d)) return cudlaErrorCuda;
        auto ptr=static_cast<CUdeviceptr>(reinterpret_cast<uintptr_t>(pointer));
        unsigned long long id=0;
        if(!allocation(*d,ptr,size,&id)) return cudlaErrorInvalidAddress;
        for(const auto& entry:d->memory) if(entry.second.pointer==ptr) return cudlaErrorMemoryRegistered;
        if(d->memory.size()>=4096) return cudlaErrorOutOfResources;
        auto key=token(); d->memory.emplace(key,Registration{ptr,size,id});
        *out=reinterpret_cast<uint64_t*>(key); return cudlaSuccess;
    });
}
cudlaStatus cudlaDrvMemUnregister(cudlaDevHandle handle,const uint64_t *pointer) {
    return boundary([&]() -> cudlaStatus {
        Device *d=device(handle); if(!d) return cudlaErrorInvalidDevice;
        if(!d->memory.erase(reinterpret_cast<uintptr_t>(pointer))) return cudlaErrorInvalidAddress;
        return cudlaSuccess;
    });
}
cudlaStatus cudlaDrvGetLastError(cudlaDevHandle handle) {
    return boundary([&]() -> cudlaStatus {
        Device *d=device(handle); return d?d->last:cudlaErrorInvalidDevice;
    });
}
cudlaStatus cudlaDrvSubmitTask(cudlaDevHandle handle,const cudlaTask *tasks,uint32_t count,void *raw_stream,uint32_t flags) {
    return boundary([&]() -> cudlaStatus {
        Device *d=device(handle); if(!d) return cudlaErrorInvalidDevice;
        if(d->last!=cudlaSuccess) return d->last;
        if(!tasks || !count) return cudlaErrorInvalidParam;
        if(count!=1 || flags || tasks->waitEvents || tasks->signalEvents) return cudlaErrorUnsupportedOperation;
        Module *m=module(tasks->moduleHandle);
        if(!m || m->owner!=reinterpret_cast<uintptr_t>(handle)) return cudlaErrorInvalidModule;
        if(!tasks->inputTensor || !tasks->outputTensor || tasks->numInputTensors!=m->model.inputs.size() ||
           tasks->numOutputTensors!=m->model.outputs.size()) return cudlaErrorInvalidParam;
        if(!current(*d)) return cudlaErrorCuda;
        auto stream=static_cast<CUstream>(raw_stream);
        CUstreamCaptureStatus capture; CUcontext stream_context=nullptr;
        if(cuStreamIsCapturing(stream,&capture)!=CUDA_SUCCESS) return cudlaErrorCuda;
        if(capture!=CU_STREAM_CAPTURE_STATUS_NONE) return cudlaErrorUnsupportedOperation;
        if(cuStreamGetCtx(stream,&stream_context)!=CUDA_SUCCESS || stream_context!=d->context) return cudlaErrorCuda;
        std::vector<Registration> inputs,outputs;
        for(bool output:{false,true}) {
            const auto& tensors=output?m->model.outputs:m->model.inputs;
            auto& bindings=output?outputs:inputs;
            for(size_t i=0;i<tensors.size();i++) {
                auto p=output?tasks->outputTensor[i]:tasks->inputTensor[i];
                auto it=d->memory.find(reinterpret_cast<uintptr_t>(p));
                if(it==d->memory.end() || it->second.bytes<tensors[i].desc.size) return cudlaErrorInvalidAddress;
                unsigned long long id=0;
                if(!allocation(*d,it->second.pointer,it->second.bytes,&id) || id!=it->second.buffer_id) return cudlaErrorInvalidAddress;
                bindings.push_back(it->second);
            }
        }
        n1x_dla_accel_info info{}; int err=n1x_dla_info(d->fd,&info);
        if(err) return cudlaErrorOs;
        auto required=(m->model.image.size()+8192+4095)&~size_t(4095);
        bool reuse=m->retained_job && (info.capabilities&N1X_DLA_CAP_REEXEC_IO);
        if(!reuse && (info.retained_bytes>info.retain_limit || required>info.retain_limit-info.retained_bytes ||
           info.retained_jobs>=info.max_jobs)) return cudlaErrorOutOfResources;
        HostImage host;
        if(quarantine_count==quarantined.size() ||
           m->model.image.size()>(64U<<20)-quarantine_bytes) return cudlaErrorOutOfResources;
        if(cuMemHostAlloc(&host.data,m->model.image.size(),0)!=CUDA_SUCCESS) return cudlaErrorOutOfResources;
        auto image=static_cast<uint8_t*>(host.data);
        std::memcpy(image,m->model.image.data(),m->model.image.size());
        for(const auto& tensor:m->model.outputs) std::memset(image+tensor.offset,0xff,tensor.desc.size);
        bool copy_error=false;
        for(size_t i=0;i<inputs.size();i++) {
            const auto& t=m->model.inputs[i];
            if(cuMemcpyDtoHAsync(image+t.offset,inputs[i].pointer,t.desc.size,stream)!=CUDA_SUCCESS) {copy_error=true; break;}
        }
        // Always drain queued copies before releasing their pinned storage,
        // including when a subsequent copy fails. No EXEC has occurred yet.
        if(cuStreamSynchronize(stream)!=CUDA_SUCCESS) {
            host.quarantine(m->model.image.size()); return d->last=cudlaErrorCuda;
        }
        if(copy_error) return d->last=cudlaErrorCuda;
        n1x_dla_accel_exec result{};
        std::vector<n1x_dla_accel_range> writable;
        for(const auto *tensors:{&m->model.inputs,&m->model.outputs})
            for(const auto& tensor:*tensors) writable.push_back({tensor.offset,tensor.desc.size});
        if(reuse) {
            std::vector<n1x_dla_accel_patch> patches;
            for(const auto *tensors:{&m->model.inputs,&m->model.outputs})
                for(const auto& tensor:*tensors)
                    patches.push_back({tensor.offset,tensor.desc.size,reinterpret_cast<uintptr_t>(image+tensor.offset)});
            err=n1x_dla_resubmit(d->fd,m->retained_job,patches.data(),patches.size(),&result);
        } else {
            err=n1x_dla_submit(d->fd,image,m->model.image.size(),m->model.ranges.data(),m->model.ranges.size(),
                               writable.data(),writable.size(),&result);
        }
        if(err) return d->last=kernel_error(err); // Never retry an uncertain EXEC.
        if(info.capabilities&N1X_DLA_CAP_REEXEC_IO) m->retained_job=result.job_id;
        for(const auto& t:m->model.outputs) {
            err=n1x_dla_read(d->fd,result.job_id,t.offset,image+t.offset,t.desc.size);
            if(err) return d->last=kernel_error(err);
        }
        for(size_t i=0;i<outputs.size();i++) {
            const auto& t=m->model.outputs[i];
            if(cuMemcpyHtoDAsync(outputs[i].pointer,image+t.offset,t.desc.size,stream)!=CUDA_SUCCESS) {copy_error=true; break;}
        }
        if(cuStreamSynchronize(stream)!=CUDA_SUCCESS) {
            host.quarantine(m->model.image.size()); return d->last=cudlaErrorCuda;
        }
        if(copy_error) return d->last=cudlaErrorCuda;
        return cudlaSuccess;
    });
}
// Every name is required by NVIDIA's frontend even when a feature is absent.
cudlaStatus cudlaDrvImportExternalMemory(cudlaDevHandle,const cudlaExternalMemoryHandleDesc*,uint64_t**,uint32_t) {
    return cudlaErrorUnsupportedOperation;
}
cudlaStatus cudlaDrvImportExternalSemaphore(cudlaDevHandle,const cudlaExternalSemaphoreHandleDesc*,uint64_t**,uint32_t) {
    return cudlaErrorUnsupportedOperation;
}
cudlaStatus cudlaDrvGetNvSciSyncAttributes(uint64_t*,uint32_t) { return cudlaErrorUnsupportedOperation; }
cudlaStatus cudlaDrvSetTaskTimeoutInMs(cudlaDevHandle,uint32_t) { return cudlaErrorUnsupportedOperation; }
cudlaStatus cudlaDrvGetExternalExportTable(const cudlaExternalEtbl**,uint32_t) { return cudlaErrorUnsupportedOperation; }
// Private prototype/table not yet recovered. This entry does not inspect
// arguments or write through pointers; it consistently declines the feature.
cudlaStatus cudlaDrvGetExportTable(...) { return cudlaErrorUnsupportedOperation; }
}
