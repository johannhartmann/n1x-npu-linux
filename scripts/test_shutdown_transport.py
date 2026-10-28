#!/usr/bin/env python3
"""Join actual Linux shutdown handlers and HFRP transport; no hardware access.

Kernel lifetime/DMA edges and firmware responses are modeled. This tests their
integration, not the physical meaning of firmware replies or warm restart.
"""
import ctypes as C
import errno
from pathlib import Path
import subprocess
import tempfile

from test_linux_shutdown import ROOT, shutdown_harness
from test_hfrp_channel import IO
from test_hfrp_stop import StopPeer


def main():
    source = shutdown_harness()
    # Keep the real bootstrap stop/refcount code and the shared modeled kernel
    # edges, replacing only the isolated protocol stub with the actual C code.
    start = source.index('int n1x_hfrp_query_power_state(')
    # The one-line query stub ends on its declaration line.
    end = source.index('return -EIO;}', start)+len('return -EIO;}')
    source = source[:start]+source[end:]
    start = source.index('static int n1x_boot_hfrp_read(')
    end = source.index('static void dma_free_coherent(', start)
    source = source[:start]+r'''
static const struct n1x_dla_io *peer;
static int n1x_boot_hfrp_read(void *ctx,u32 off,u32 *out)
{assert(boot_lock && ctx==&dla_ep && peer);return peer->read32(peer->context,off,out);}
static int n1x_boot_hfrp_write(void *ctx,u32 off,u32 value)
{assert(boot_lock && ctx==&dla_ep && peer);return peer->write32(peer->context,off,value);}
static void n1x_boot_delay(void *ctx,u32 us)
{assert(boot_lock && ctx==&dla_ep && peer);peer->delay_us(peer->context,us);}
''' + source[end:]
    source += r'''
int joined_stop(const struct n1x_dla_io *io)
{
 reset();peer=io;route=0x010801d0;mask=0x01001e05;
 boot_hfrp.channel.state=N1X_HFRP_READY;
 boot_hfrp.channel.next_sequence=9;
 boot_hfrp.channel.pending_sequence=N1X_HFRP_NO_SEQUENCE;
 boot_hfrp.version=(struct n1x_hfrp_version){.valid=1,.major=1,.minor=5};
 return n1x_boot_stop_device(&device);
}
int repeat_stop(void) {return n1x_boot_stop_device(&device);}
int released(void)
{
 if(boot_shutdown.released) {
  assert(boot_shutdown.hardware_off && dma_frees==7);
  assert(pm_puts==3 && !module_refs && !device.refs && !sroot_device.refs);
  assert(!boot_hfrp_held && !boot_upload_held && !accel.bytes);
  return 1;
 }
 assert(!dma_frees && !pm_puts && module_refs==2 && device.refs==2 && sroot_device.refs==1);
 assert(boot_hfrp_held && boot_upload_held && accel.bytes==4096);
 return 0;
}
'''
    with tempfile.TemporaryDirectory(prefix='n1x-joined-stop-') as tmp:
        path, library = Path(tmp)/'joined.c', Path(tmp)/'joined.so'
        path.write_text(source)
        subprocess.run(['cc', '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
                        '-Wno-unused-parameter', '-Wno-unused-variable', '-Wno-misleading-indentation',
                        '-Wno-sign-compare', '-I'+str(ROOT/'include'), str(path),
                        str(ROOT/'driver/n1x_hfrp.c'), '-o', str(library)], check=True)
        lib = C.CDLL(str(library))
    lib.joined_stop.argtypes = [C.POINTER(IO)]
    cases = 0

    def check(peer, expected):
        nonlocal cases
        code = lib.joined_stop(C.byref(peer.io))
        assert code == expected, (code, expected)
        assert lib.released() == (not expected)
        count = len(peer.trace)
        assert lib.repeat_stop() == expected and len(peer.trace) == count
        assert lib.released() == (not expected)
        assert not peer.errors, peer.errors
        cases += 1

    # The raw mask retains bit 24; success still requires actual POWER_CONTROL
    # and state-query exchanges, before any of the seven DMA frees occur.
    for state in range(256):
        check(StopPeer(state=bytes([state])), 0 if state&7 == 7 else -errno.EBUSY)
    for options, code in (
        ({'off': b'\x8c\0'}, -errno.EBADMSG),
        ({'off': b''}, -errno.EBADMSG),
        ({'state': b''}, -errno.EBADMSG),
        ({'query_status': 1}, -errno.EREMOTEIO),
        ({'never_reply': True}, -errno.ETIMEDOUT),
        ({'wrong': True}, -errno.EPROTO),
        ({'async_before': True}, -errno.EOPNOTSUPP),
    ):
        check(StopPeer(**options), code)
    baseline = StopPeer()
    check(baseline, 0)
    for index in range(len(baseline.trace)):
        for after in (False, True):
            peer = StopPeer()
            peer.fail_at, peer.fail_after_write = index, after
            check(peer, -errno.EIO)
    print(f'{cases} joined Linux shutdown/HFRP transport cases passed; DMA/PM/module retention on every failure; no hardware')


if __name__ == '__main__':
    main()
