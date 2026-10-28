// SPDX-License-Identifier: MIT
// Native counterpart of loadable.py's checked single-task NVDA materializer.
#include "cudla_model.hpp"
#include <algorithm>
#include <array>
#include <cstring>
#include <map>
#include <set>
#include <stdexcept>
#include <string>
#include <utility>

namespace n1x {
namespace {
constexpr uint64_t max_image = 32U << 20;
void need(bool ok, const char *message) {
    if (!ok) throw std::invalid_argument(message);
}
struct Reader;
struct Table {
    Reader *r;
    uint64_t at, size;
    std::vector<uint16_t> offsets;
    uint64_t field(size_t index, size_t width) const;
    uint64_t value(size_t index, size_t width, uint64_t fallback=0) const;
    std::array<uint8_t,3> version(size_t index) const;
    std::vector<uint64_t> vector(size_t index, size_t width, uint64_t limit=4096) const;
    std::vector<Table> tables(size_t index, size_t fields) const;
    std::vector<uint64_t> numbers(size_t index, size_t width) const;
    std::string name(size_t index) const;
    std::vector<std::string> names(size_t index) const;
};
struct Reader {
    const uint8_t *data;
    uint64_t size, records=65536, name_bytes=1U<<20;
    void span(uint64_t at, uint64_t count) const {
        need(at <= size && count <= size-at, "field outside file");
    }
    uint64_t number(uint64_t at, size_t width) const {
        span(at,width);
        uint64_t result=0;
        for (size_t i=0;i<width;i++) result |= uint64_t(data[at+i]) << (8*i);
        return result;
    }
    void consume(uint64_t count) {
        need(count <= records,"metadata traversal budget"); records-=count;
    }
    uint64_t indirect(uint64_t at) const {
        uint64_t delta=number(at,4);
        need(delta>=4,"invalid forward offset"); span(at+delta,4); return at+delta;
    }
    Table table(uint64_t at, size_t fields) {
        consume(1); need(at>=8,"table overlaps header");
        int64_t distance=static_cast<int32_t>(number(at,4));
        int64_t v=static_cast<int64_t>(at)-distance;
        need(v>=8 && uint64_t(v)!=at,"invalid vtable");
        uint64_t length=number(v,2), bytes=number(v+2,2);
        need(length>=4 && length<=4+fields*2 && !(length%2) && bytes>=4,"table header");
        need(uint64_t(v)+length<=at || at+bytes<=uint64_t(v),"overlapping vtable");
        span(v,length); span(at,bytes);
        Table result{this,at,bytes,{}};
        for(uint64_t i=4;i<length;i+=2) {
            auto offset=number(v+i,2);
            need(!offset || (offset>=4 && offset<bytes),"table field offset");
            result.offsets.push_back(offset);
        }
        return result;
    }
    std::string string(uint64_t at) {
        auto target=indirect(at), bytes=number(target,4);
        need(bytes && bytes<=4096 && bytes<=name_bytes,"name size");
        name_bytes-=bytes; span(target+4,bytes+1);
        need(!data[target+4+bytes],"name termination");
        std::string result(reinterpret_cast<const char*>(data+target+4),bytes);
        need(result.find('\0')==std::string::npos,"embedded name terminator");
        return result;
    }
};
uint64_t Table::field(size_t index,size_t width) const {
    auto offset=index<offsets.size()?offsets[index]:0;
    if (!offset) return 0;
    need(width<=size-offset,"field width"); return at+offset;
}
uint64_t Table::value(size_t index,size_t width,uint64_t fallback) const {
    auto f=field(index,width); return f?r->number(f,width):fallback;
}
std::array<uint8_t,3> Table::version(size_t index) const {
    auto f=field(index,3); need(f,"missing version");
    return {r->data[f],r->data[f+1],r->data[f+2]};
}
std::vector<uint64_t> Table::vector(size_t index,size_t width,uint64_t limit) const {
    auto f=field(index,4); if(!f) return {};
    auto target=r->indirect(f), count=r->number(target,4);
    need(count<=limit,"vector limit"); r->consume(count);
    r->span(target+4,count*width);
    std::vector<uint64_t> out; out.reserve(count);
    for(uint64_t i=0;i<count;i++) out.push_back(target+4+i*width);
    return out;
}
std::vector<Table> Table::tables(size_t index,size_t fields) const {
    std::vector<Table> out;
    for(auto at:vector(index,4)) out.push_back(r->table(r->indirect(at),fields));
    return out;
}
std::vector<uint64_t> Table::numbers(size_t index,size_t width) const {
    std::vector<uint64_t> out;
    for(auto at:vector(index,width)) out.push_back(r->number(at,width));
    return out;
}
std::string Table::name(size_t index) const {
    auto f=field(index,4); need(f,"missing name"); return r->string(f);
}
std::vector<std::string> Table::names(size_t index) const {
    std::vector<std::string> out;
    for(auto at:vector(index,4)) out.push_back(r->string(at));
    return out;
}
void dense(const std::vector<Table>& tables) {
    for(size_t i=0;i<tables.size();i++) need(tables[i].value(0,2)==i,"non-dense IDs");
}
void extent(uint64_t offset,uint64_t bytes,uint64_t capacity) {
    need(bytes && offset<capacity && bytes<=capacity-offset,"range outside memory");
}
struct Blob { uint64_t offset,bytes,interface; };
struct Memory {
    uint64_t offset,bytes,flags,bind,tensor;
    bool scratch;
};
}

Model::Model(const uint8_t *data,size_t bytes) {
    need(data && bytes>=8 && bytes<=(64U<<20),"invalid loadable size");
    need(!std::memcmp(data+4,"NVDA",4),"NVDA identifier");
    Reader r{data,bytes};
    auto root=r.table(r.number(0,4),10);
    need(root.version(0)==std::array<uint8_t,3>{0,9,0} &&
         root.version(9)==std::array<uint8_t,3>{3,0,0},"loadable version");
    need(root.vector(7,4).empty(),"relocations unsupported");
    auto tasks=root.tables(1,6), submits=root.tables(8,2), events=root.tables(4,5);
    need(tasks.size()==1 && submits.size()==1 && events.size()==1,"single task required");
    const auto &task=tasks[0], &event=events[0];
    need(task.value(0,2)==0 && task.value(1,4)==1 &&
         (task.value(2,2)==0xffff || task.value(2,2)==0),"task target");
    need(task.numbers(4,2).empty() && task.numbers(5,2)==std::vector<uint64_t>{0},"task actions");
    need(submits[0].value(0,2)==0 && submits[0].numbers(1,2)==std::vector<uint64_t>{0},"submit order");
    need(event.value(0,2)==0 && event.value(1,1)==0 && event.value(2,2)==0 &&
         event.value(3,4)==1 && event.value(4,1)==1,"completion event");
    std::map<std::string,Blob> catalog;
    std::map<std::string,uint64_t> pools;
    for(const auto& blob:root.tables(5,7)) {
        auto name=blob.name(0); auto f=blob.field(5,4);
        uint64_t at=0, size=0;
        if(f) { auto target=r.indirect(f); size=r.number(target,4); at=target+4; r.span(at,size); }
        need(size<=max_image && blob.value(1,8)==size && !catalog.count(name),"content size/name");
        auto interface=blob.value(2,4);
        need(interface<=1,"content interface");
        need(blob.version(4)==(interface?std::array<uint8_t,3>{3,0,0}:std::array<uint8_t,3>{0,0,0}) &&
             blob.version(6)==(interface?std::array<uint8_t,3>{3,16,0}:std::array<uint8_t,3>{0,0,0}),"content version");
        if(name.rfind("property_",0)==0) {
            need((name=="property_global_dram_pool_size" || name=="property_local_dram_pool_size" ||
                  name=="property_local_cvsram_pool_size") && !interface && size==8,"pool property");
            auto amount=r.number(at,8); need(amount<=max_image,"pool size");
            need(name!="property_local_cvsram_pool_size" || !amount,"SRAM unsupported");
            pools[name]=amount;
        }
        catalog.emplace(name,Blob{at,size,interface});
    }
    auto memories=root.tables(2,9), addresses=root.tables(3,4);
    need(!memories.empty() && !addresses.empty(),"missing memory"); dense(memories); dense(addresses);
    std::vector<Memory> layout;
    struct Copy {uint64_t destination,source,bytes;};
    std::vector<Copy> copies;
    struct Constants {size_t memory;uint64_t flags,end;bool valid;};
    std::vector<Constants> constants;
    uint64_t cursor=0;
    for(const auto& memory:memories) {
        auto size=memory.value(3,8), alignment=memory.value(4,4), flags=memory.value(2,2);
        need(memory.value(1,1)==0 && (flags&1) && !(flags&~15U),"memory flags/domain");
        need(alignment && alignment<=4096 && !(alignment&(alignment-1)) && size && size<=max_image,"memory size/alignment");
        alignment=std::max<uint64_t>(alignment,256);
        uint64_t offset=(cursor+alignment-1)&~(alignment-1);
        uint64_t backing=(std::max<uint64_t>(size,256)+255)&~uint64_t(255);
        cursor=offset+backing; need(cursor<=max_image,"image limit");
        auto names=memory.names(5); auto offsets=memory.numbers(6,8);
        need(names.size()==offsets.size(),"content offsets");
        layout.push_back({offset,size,flags,memory.value(7,2),memory.value(8,2),flags==1&&names.empty()});
        size_t raw=0; uint64_t high=0; bool property=false;
        if(flags&2) for(size_t i=0;i<names.size();i++) {
            need(catalog.count(names[i]),"missing content"); const auto &b=catalog.at(names[i]);
            need(offsets[i]<=size && b.bytes<=size-offsets[i],"initializer extent");
            copies.push_back({offset+offsets[i],b.offset,b.bytes});
            if(!b.interface) { raw++; high=std::max(high,offsets[i]+b.bytes); }
            property |= names[i].rfind("property_",0)==0;
        }
        if(raw) constants.push_back({layout.size()-1,flags,high,raw==names.size()&&!property});
    }
    std::vector<n1x_dla_accel_range> resolved;
    for(const auto& address:addresses) {
        auto id=address.value(1,2), offset=address.value(2,8), size=address.value(3,8);
        need(id<layout.size(),"address memory ID"); extent(offset,size,layout[id].bytes);
        resolved.push_back({layout[id].offset+offset,size});
    }
    auto ids=task.numbers(3,2); need(!ids.empty() && ids.size()<=512,"address count");
    bool local_covered=false;
    for(auto id:ids) {
        need(id<resolved.size(),"task address ID"); ranges.push_back(resolved[id]);
        const auto& a=addresses[id];
        local_covered |= layout[a.value(1,2)].scratch && !a.value(2,8) &&
                         resolved[id].bytes>=pools["property_local_dram_pool_size"];
    }
    need(!pools["property_local_dram_pool_size"] || local_covered,"unrepresented local pool");
    auto global=pools["property_global_dram_pool_size"];
    if(global) {
        need(constants.size()==1 && constants[0].flags==3 && constants[0].valid &&
             constants[0].end==global,"global constant pool");
        auto start=layout[constants[0].memory].offset;
        need(std::any_of(ranges.begin(),ranges.end(),[&](const auto& a){
            return a.offset<=start && start+global<=a.offset+a.bytes;
        }),"constant pool coverage");
    }
    auto tensors=root.tables(6,22); std::set<std::string> names;
    std::set<std::pair<bool,uint64_t>> bindings;
    for(size_t i=0;i<tensors.size();i++) {
        const auto &t=tensors[i]; auto name=t.name(0);
        auto id=t.value(1,2), memory_id=t.value(2,2);
        need(id==i && names.insert(name).second && memory_id<layout.size(),"tensor identity");
        const auto& mem=layout[memory_id];
        need(!(mem.flags&12) || id==mem.tensor,"tensor binding");
        auto size=t.value(3,8), offset=t.value(4,8); extent(offset,size,mem.bytes);
        Tensor out; need(name.size()<sizeof(out.desc.name),"tensor name too long");
        std::memcpy(out.desc.name,name.data(),name.size()); out.desc.size=size;
        uint64_t shape[4];
        for(size_t j=0;j<4;j++) { auto d=t.value(10+j,4); need(d && d<=INT32_MAX,"static shape"); shape[j]=d; }
        need(shape[0]==1,"batch one required");
        out.desc.n=shape[0]; out.desc.c=shape[1]; out.desc.h=shape[2]; out.desc.w=shape[3];
        out.desc.dataFormat=t.value(5,1); out.desc.dataType=t.value(6,1);
        out.desc.dataCategory=t.value(7,1); out.desc.pixelFormat=t.value(8,1); out.desc.pixelMapping=t.value(9,1);
        static_assert(CUDLA_LOADABLE_TENSOR_DESC_NUM_STRIDES==8);
        for(size_t j=0;j<8;j++) out.desc.stride[j]=t.value(14+j,4);
        out.offset=mem.offset+offset; out.bind_id=mem.bind;
        if(mem.flags&4) { need(bindings.emplace(true,mem.bind).second,"duplicate input binding"); inputs.push_back(out); }
        if(mem.flags&8) { need(bindings.emplace(false,mem.bind).second,"duplicate output binding"); outputs.push_back(out); }
    }
    for(size_t i=0;i<layout.size();i++) if(layout[i].flags&12)
        need(layout[i].tensor<tensors.size() && tensors[layout[i].tensor].value(2,2)==i,"memory tensor mismatch");
    need(!inputs.empty() && !outputs.empty(),"missing input/output");
    auto order=[](const Tensor& a,const Tensor& b){return a.bind_id<b.bind_id;};
    std::sort(inputs.begin(),inputs.end(),order); std::sort(outputs.begin(),outputs.end(),order);
    // cuDLA binds arrays by index. Reject sparse bindings rather than guessing.
    for(size_t i=0;i<inputs.size();i++) need(inputs[i].bind_id==i,"sparse input bindings");
    for(size_t i=0;i<outputs.size();i++) need(outputs[i].bind_id==i,"sparse output bindings");
    image.resize(cursor);
    for(const auto &copy:copies) if(copy.bytes) std::memcpy(image.data()+copy.destination,data+copy.source,copy.bytes);
}
}
