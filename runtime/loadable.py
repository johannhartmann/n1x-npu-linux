"""Bounded userspace NVDA 0.9.0 materializer for the recovered N1X profile.

No compiler, device access or instruction-stream validation. Supported subset:
one DLA task, DRAM, no relocations/pre-waits, one terminal completion event.
Metadata layouts come from the original 617.42 Windows runtime; the additional
version fields are checked explicitly. Bytes within model instructions remain
trusted, just as required by the experimental RAWIO-only kernel interface.
"""
import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import struct

MAX_IMAGE = 32 << 20
MAX_FILE = 64 << 20
MAX_RECORDS = 4096


class LoadableError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise LoadableError(message)


class Reader:
    def __init__(self, data):
        require(8 <= len(data) <= MAX_FILE, 'invalid loadable length')
        self.data = memoryview(bytes(data))
        self.records_left = 65536
        self.names_left = 1 << 20
        require(self.data[4:8] == b'NVDA', 'missing NVDA identifier')

    def span(self, at, size):
        require(0 <= at <= len(self.data) and 0 <= size <= len(self.data)-at,
                'field outside file')
        return self.data[at:at+size]

    def number(self, fmt, at):
        return struct.unpack(fmt, self.span(at, struct.calcsize(fmt)))[0]

    def indirect(self, at):
        delta = self.number('<I', at)
        require(delta >= 4, 'invalid forward offset')
        target = at + delta
        self.span(target, 4)
        return target

    def consume(self, count):
        require(count <= self.records_left, 'metadata traversal budget exceeded')
        self.records_left -= count

    def table(self, at, fields):
        self.consume(1)
        require(at >= 8, 'table overlaps file header')
        vtable = at - self.number('<i', at)
        require(vtable >= 8 and vtable != at, 'invalid vtable location')
        length, size = self.number('<H', vtable), self.number('<H', vtable+2)
        require(4 <= length <= 4+2*fields and length % 2 == 0 and size >= 4, 'invalid or extended table header')
        require(vtable+length <= at or at+size <= vtable, 'vtable overlaps object')
        self.span(vtable, length)
        self.span(at, size)
        offsets = [self.number('<H', vtable+4+2*i) for i in range((length-4)//2)]
        require(all(not o or 4 <= o < size for o in offsets), 'field outside table')
        require(not any(offsets[fields:]), 'unsupported table extension')
        return Table(self, at, size, offsets)

    def string_at(self, at):
        target = self.indirect(at)
        size = self.number('<I', target)
        require(0 < size <= 4096, 'invalid name length')
        require(size <= self.names_left, 'name budget exceeded')
        self.names_left -= size
        raw = bytes(self.span(target+4, size))
        require(self.number('<B', target+4+size) == 0 and b'\0' not in raw, 'invalid name')
        try:
            return raw.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise LoadableError('invalid UTF-8 name') from exc


class Table:
    def __init__(self, reader, at, size, offsets):
        self.reader, self.at, self.size, self.offsets = reader, at, size, offsets

    def field(self, index, width):
        offset = self.offsets[index] if index < len(self.offsets) else 0
        if not offset:
            return None
        require(width <= self.size-offset, 'scalar extends past table')
        return self.at+offset

    def value(self, index, fmt, default=0):
        field = self.field(index, struct.calcsize(fmt))
        return default if field is None else self.reader.number(fmt, field)

    def version(self, index):
        field = self.field(index, 3)
        require(field is not None, 'required version is absent')
        return tuple(self.reader.span(field, 3))

    def vector(self, index, width, limit=MAX_RECORDS):
        field = self.field(index, 4)
        if field is None:
            return range(0)
        target = self.reader.indirect(field)
        count = self.reader.number('<I', target)
        require(count <= limit, 'vector exceeds supported limit')
        if limit != MAX_IMAGE:
            self.reader.consume(count)
        self.reader.span(target+4, count*width)
        return range(target+4, target+4+count*width, width)

    def tables(self, index, fields):
        return [self.reader.table(self.reader.indirect(at), fields) for at in self.vector(index, 4)]

    def numbers(self, index, fmt):
        return [self.reader.number(fmt, at) for at in self.vector(index, struct.calcsize(fmt))]

    def name(self, index):
        field = self.field(index, 4)
        require(field is not None, 'required name is absent')
        return self.reader.string_at(field)

    def names(self, index):
        return [self.reader.string_at(at) for at in self.vector(index, 4)]

    def blob(self, index):
        span = self.vector(index, 1, MAX_IMAGE)
        return self.reader.span(span.start, len(span))


def dense(tables):
    require(all(t.value(0, '<H') == i for i, t in enumerate(tables)), 'IDs must be dense and ordered')


def extent(offset, size, capacity):
    require(size > 0 and 0 <= offset < capacity and size <= capacity-offset,
            'logical range outside memory')


@dataclass(frozen=True)
class Tensor:
    name: str
    id: int
    memory_id: int
    offset: int  # Relative to the prepared image, not a DMA address.
    bytes: int
    shape: tuple
    strides: tuple
    format_codes: tuple  # Preserve modern enum values; do not guess old-schema names.
    input: bool
    output: bool


class Model:
    def __init__(self, data):
        reader = Reader(data)
        root = reader.table(reader.number('<I', 0), 10)
        require(root.version(0) == (0, 9, 0), 'unsupported serialization version')
        require(root.version(9) == (3, 0, 0), 'unsupported runtime interface version')
        require(not root.vector(7, 4), 'relocations are not implemented')
        tasks, submits, events = root.tables(1, 6), root.tables(8, 2), root.tables(4, 5)
        require(len(tasks) == len(submits) == len(events) == 1, 'expected one task/submit/completion event')
        task, submit, event = tasks[0], submits[0], events[0]
        require(task.value(0, '<H') == 0 and task.value(1, '<I') == 1 and
                task.value(2, '<h') in (-1, 0), 'unsupported task target')
        require(not task.numbers(4, '<H') and task.numbers(5, '<H') == [0], 'unsupported task actions')
        require(submit.value(0, '<H') == 0 and submit.numbers(1, '<H') == [0], 'unsupported submit order')
        require([event.value(i, fmt) for i, fmt in enumerate(('<H', '<B', '<H', '<I', '<B'))]
                == [0, 0, 0, 1, 1], 'unsupported completion event')
        catalog = {}
        properties = {'property_global_dram_pool_size', 'property_local_cvsram_pool_size',
                      'property_local_dram_pool_size'}
        self.memory_pools = {}
        self.blob_versions = {}
        for blob in root.tables(5, 7):
            name, payload = blob.name(0), blob.blob(5)
            require(name not in catalog and blob.value(1, '<Q') == len(payload), 'duplicate name or blob size mismatch')
            interface = blob.value(2, '<I')
            require(interface in (0, 1), 'unsupported content interface')
            require(blob.version(4) == ((3, 0, 0) if interface else (0, 0, 0)), 'unsupported content version')
            auxiliary = blob.version(6)
            require(auxiliary == ((3, 16, 0) if interface else (0, 0, 0)), 'unsupported auxiliary content version')
            if name.startswith('property_'):
                require(name in properties and interface == 0 and len(payload) == 8,
                        'unknown or malformed memory-pool property')
                pool_size = struct.unpack('<Q', payload)[0]
                require(pool_size <= MAX_IMAGE, 'memory-pool property exceeds image limit')
                require(name != 'property_local_cvsram_pool_size' or pool_size == 0,
                        'nonzero local SRAM pool is not implemented')
                self.memory_pools[name] = pool_size
            catalog[name] = payload
            self.blob_versions[name] = dict(interface=interface, sub_interface=blob.value(3, '<I'),
                                           version=blob.version(4), auxiliary_version=auxiliary)
        memories, addresses = root.tables(2, 9), root.tables(3, 4)
        require(0 < len(memories) <= MAX_RECORDS and addresses, 'missing memory/address table')
        dense(memories)
        dense(addresses)
        layout, initializers, cursor = [], [], 0
        constant_memories = []
        scratch_memories = set()
        for memory in memories:
            size, alignment, flags = memory.value(3, '<Q'), memory.value(4, '<I'), memory.value(2, '<H')
            require(memory.value(1, '<B') == 0 and flags & 1 and not flags & ~15, 'unsupported memory domain/flags')
            require(0 < alignment <= 4096 and alignment & (alignment-1) == 0, 'unsupported memory alignment')
            require(0 < size <= MAX_IMAGE, 'invalid memory size')
            alignment = max(alignment, 256)
            offset = (cursor+alignment-1) & -alignment
            backing = (max(size, 256)+255) & ~255
            cursor = offset+backing
            require(cursor <= MAX_IMAGE, 'prepared image exceeds driver limit')
            layout.append(dict(offset=offset, bytes=size, backing=backing, flags=flags,
                               bind_id=memory.value(7, '<H'), tensor_id=memory.value(8, '<H')))
            names, offsets = memory.names(5), memory.numbers(6, '<Q')
            require(len(names) == len(offsets), 'content name/offset length mismatch')
            if flags == 1 and not names:
                scratch_memories.add(len(layout)-1)
            if flags & 2:
                raw_constants = []
                for name, content_offset in zip(names, offsets):
                    require(name in catalog, 'missing named content')
                    content = catalog[name]
                    require(content_offset <= size and len(content) <= size-content_offset, 'content exceeds logical memory')
                    initializers.append((offset+content_offset, content))
                    if self.blob_versions[name]['interface'] == 0:
                        raw_constants.append((name, content_offset+len(content)))
                if raw_constants:
                    constant_memories.append((len(layout)-1, flags, names, raw_constants))
        resolved = []
        for address in addresses:
            index = address.value(1, '<H')
            require(index < len(layout), 'invalid address memory ID')
            memory = layout[index]
            offset, size = address.value(2, '<Q'), address.value(3, '<Q')
            extent(offset, size, memory['bytes'])
            resolved.append((memory['offset']+offset, size))
        task_ids = task.numbers(3, '<H')
        require(0 < len(task_ids) <= 512 and all(i < len(resolved) for i in task_ids), 'invalid task-local address IDs')
        self.ranges = tuple(resolved[i] for i in task_ids)
        local_pool = self.memory_pools.get('property_local_dram_pool_size', 0)
        if local_pool:
            # The observed Conv->ReLU intermediate is already a DRAM memory
            # descriptor and task address. Allocate that descriptor once,
            # without inventing a second buffer from the pool-size property.
            # Only a pool covered from offset zero by one uninitialized,
            # non-I/O task range is supported. Instructions remain trusted;
            # this capacity check does not identify their indirect references.
            require(any(addresses[i].value(1, '<H') in scratch_memories and
                        addresses[i].value(2, '<Q') == 0 and
                        resolved[i][1] >= local_pool for i in task_ids),
                    'local DRAM pool is not covered by an uninitialized task range')
        global_pool = self.memory_pools.get('property_global_dram_pool_size', 0)
        if global_pool:
            # The original 0x1ec70 -> 0x20d00 path allocates from the memory
            # descriptor, then copies raw constants at their listed offsets.
            # Qualify the observed single constant-pool layout; never allocate
            # a second backing merely because its high-water property is nonzero.
            require(len(constant_memories) == 1, 'ambiguous global constant pool')
            index, flags, names, constants = constant_memories[0]
            require(flags == 3 and len(constants) == len(names) and
                    all(not name.startswith('property_') for name, _ in constants),
                    'unsupported global constant-pool contents')
            require(max(end for _, end in constants) == global_pool,
                    'global constant-pool extent mismatch')
            start = layout[index]['offset']
            require(any(offset <= start and start+global_pool <= offset+size
                        for offset, size in self.ranges),
                    'global constant pool is not covered by task addresses')
        tensors, names = [], set()
        for table in root.tables(6, 22):
            name, index, memory_id = table.name(0), table.value(1, '<H'), table.value(2, '<H')
            require(index == len(tensors) and name not in names and memory_id < len(layout), 'invalid tensor identity')
            memory = layout[memory_id]
            require(not memory['flags'] & 12 or index == memory['tensor_id'], 'ambiguous I/O tensor binding')
            size, offset = table.value(3, '<Q'), table.value(4, '<Q')
            extent(offset, size, memory['bytes'])
            shape = tuple(table.value(i, '<i') for i in range(10, 14))
            require(shape[0] == 1 and all(d > 0 for d in shape), 'only static batch-1 tensors are supported')
            tensors.append(Tensor(name, index, memory_id, memory['offset']+offset, size, shape,
                                  tuple(table.value(i, '<I') for i in range(14, 22)),
                                  tuple(table.value(i, '<B') for i in range(5, 10)),
                                  bool(memory['flags'] & 4), bool(memory['flags'] & 8)))
            names.add(name)
        bindings = set()
        for index, memory in enumerate(layout):
            for direction, flag in (('input', 4), ('output', 8)):
                if memory['flags'] & flag:
                    tensor_id = memory['tensor_id']
                    require(tensor_id < len(tensors) and tensors[tensor_id].memory_id == index,
                            'memory/tensor binding mismatch')
                    key = (direction, memory['bind_id'])
                    require(key not in bindings, 'duplicate tensor binding ID')
                    bindings.add(key)
        self.layout = tuple(layout)
        self.tensors = tuple(tensors)
        self.inputs = tuple(t for t in tensors if t.input)
        self.outputs = tuple(t for t in tensors if t.output)
        require(self.inputs and self.outputs, 'expected input and output tensors')
        # All views and extents are validated before copying any model content.
        image = bytearray(cursor)
        for offset, payload in initializers:
            image[offset:offset+len(payload)] = payload
        self.image = bytes(image)
        self.sha256 = hashlib.sha256(reader.data).hexdigest()

    @classmethod
    def from_file(cls, path):
        with Path(path).open('rb') as source:
            return cls(source.read(MAX_FILE+1))

    def prepare(self, inputs):
        require(set(inputs) == {t.name for t in self.inputs}, 'input names do not match model')
        copies = []
        for tensor in self.inputs:
            value = bytes(inputs[tensor.name])
            require(len(value) == tensor.bytes, 'input byte count does not match tensor')
            copies.append((tensor.offset, value))
        image = bytearray(self.image)
        for offset, value in copies:
            image[offset:offset+len(value)] = value
        return image, self.ranges

    def describe(self):
        return dict(sha256=self.sha256, serialization_version=[0, 9, 0], runtime_version=[3, 0, 0],
                    image_bytes=len(self.image), task_addresses=self.ranges,
                    tensors=[asdict(t) for t in self.tensors], blobs=self.blob_versions,
                    memory_pools=self.memory_pools,
                    instruction_streams_validated=False, hardware_execution=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('loadable', type=Path)
    args = parser.parse_args()
    print(json.dumps(Model.from_file(args.loadable).describe(), indent=2))
