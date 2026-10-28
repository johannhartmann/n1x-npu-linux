# Exact-result adapter for checked graph fixtures; runs only in the native sandbox.
# This supplies actual Windows translation results to the original serializer.
source scripts/capture_native_translate.gdb
python
final=pathlib.Path('results/windows-'+model+'-backend' if conv else 'results/windows-final-backend-output')
fm=json.loads((final/'manifest.json').read_text())
assert fm['passed'] and (fm['translation']['return_value'] if conv else fm['translation_return'])==0 and fm['windows_sha256']==windows_sha
assert fm['hardware_access'] is False and fm['handoff']['inputs_unchanged_after_translation']
assert fm['handoff']['network_sha256']==hashlib.sha256(network).hexdigest()
if not conv:assert fm['handoff']['stage']=='captured frontend translation descriptor'
for e in entries:
    data=bytearray(read(maps['pointer'][e['kind']],e['bytes']))
    for offset in fm['handoff'].get('pointer_offsets',{}).get(str(e['kind']),[]):
        data[offset:offset+8]=bytes(8)
    assert fm['handoff']['input_sha256'][str(e['kind'])]==hashlib.sha256(data).hexdigest()
payloads={}
for e in (fm['translation']['outputs'] if conv else fm['outputs']):
    data=(final/e['file']).read_bytes()
    assert len(data)==e['bytes'] and hashlib.sha256(data).hexdigest()==e['sha256']
    assert e['kind'] not in payloads
    payloads[e['kind']]=data
assert set(payloads)=={0,1,3,4,5,6,7}
calls=[]
expected_calls=[('sizes',7),('imatrix',0),('copy',3),('copy',0),('copy',1),('copy',5),('copy',4)]
def replay(name):
    a=[int(gdb.parse_and_eval('$x%d'%i)) for i in range(3)]
    assert a[0]==0  # The native backend-3 factory remains unavailable.
    assert len(calls)<len(expected_calls) and (name,a[1])==expected_calls[len(calls)]
    status=0
    if name=='sizes':
        assert a[1]==7 and a[2]
        inf.write_memory(a[2],struct.pack('<7I',*[len(payloads.get(k,b'')) for k in range(7)]))
    elif name=='copy':
        assert a[1] in payloads and a[2]
        data=payloads[a[1]]
        if data:inf.write_memory(a[2],data)
        else:status=8
    elif name=='imatrix':
        assert a[1]==0 and a[2]
        # Audited backend-3 path at ELF 0x6493cc-0x6493e4: with a real
        # non-null backend, success and no caller output writes. No imatrix
        # contents are synthesized. Allocation/free side effects are omitted.
    else:raise AssertionError(name)
    calls.append(dict(name=name,kind=a[1],status=status))
    gdb.execute('return (unsigned int)%d'%status)
gdb.execute('return (unsigned int)0')
end
break nvdla_mtl_get_blob_buf_sizes
commands
silent
python replay('sizes')
continue
end
break nvdla_mtl_copy_blob_buf
commands
silent
python replay('copy')
continue
end
break nvdla_mtl_copy_imatrix_out_data
commands
silent
python replay('imatrix')
continue
end
continue
python
assert int(gdb.parse_and_eval('$_exitcode'))==0
assert calls==[dict(name=name,kind=kind,status=0) for name,kind in expected_calls]
print('PACKAGING REPLAY CALLS',json.dumps(calls))
(out/'packaging-replay.json').write_text(json.dumps(dict(passed=True,hardware_access=False,
    backend_manifest_sha256=hashlib.sha256((final/'manifest.json').read_bytes()).hexdigest(),
    calls=calls,limitations=['Exact-result replay, not a native backend implementation',
    'Imatrix backend-3 no-output branch modeled from disassembly',
    'Serialized output does not prove runtime or firmware compatibility']),indent=2)+'\n')
end
