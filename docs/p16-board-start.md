# P16 BIOS-300 board policy and HFRP startup

The actual P16 policy is now available. The original NVIDIA 617.42 Windows
parser and complete policy initializer both return zero on the physical ROM
capture. All fields used by the native parser and the derived gating policy
match the original code. This resolves the missing board-policy input; it
does not establish a successful hardware boot.

## Reproducible read-only acquisition

Windows RVA `0xd3d2c0` reads physical locator `0x1182bc80` and maps
`((locator << 12) + 0x80000600) & 0xffffffff` for exactly `0x110000` bytes.
On this P16 H7607BA, BIOS H7607BA.300, kernel
`7.2.8-23-omarchy-p16-asus-npu2`, the locator is `0x39600`, selecting
`0xb9600600`. The whole window is inside reserved `b9600000-b97fffff`.

`scripts/read_p16_vbios_locator.c` restricts itself to that exact machine,
kernel, locator address and observed reserved window. The default operation
only reads the locator. `--capture` reads the bounded window twice, compares
the copies, and creates a new mode-0600 file. It neither writes registers nor
binds a driver. No arbitrary address/size option exists.

```sh
cc -O2 -Wall -Wextra -Werror scripts/read_p16_vbios_locator.c \
  -o results/read_p16_vbios_locator
pkexec "$PWD/results/read_p16_vbios_locator" \
  --capture "$PWD/results/p16-bios300-physical.rom"
venv/bin/python scripts/verify_p16_board_policy.py \
  results/p16-bios300-physical.rom > results/p16-board-policy-verified.json
```

Do not repeat acquisition over an existing file. The captured vendor bytes
stay ignored, not redistributed. The full-window SHA256 is
`a82af14d15effa98a75b19d9135421c7c27f20cca7398d0b0a7ba87547b8b64c`
(1,114,112 bytes). The preceding aligned PCI image begins at window offset
`0x27f00`; the BIT header is at `0x280b0` and its ASCII letters at `0x280b2`.
The window need not begin with `55aa`. The directory is at `0x283b0`;
frequency, tuning and gating tables are at `0x5272b`, `0x5273d`, `0x5275d`.

| Parsed input | Actual value |
| --- | --- |
| Operating / entry / exit frequency | 430,000 kHz each |
| Power-cap word / nine tuning words / gating word | All zero in the actual table |
| Frequency flags / gating flag | `[1,1]` / `1` |
| Gating bytes | `[1,1,3]` |
| Feature mask / general word | `0` / `512` |
| General header / allocation words | `[32,14,305]` / `[16,8,0,0]` |
| Derived request mask | `0x777` |
| Derived policy bytes | `[0,1,1,0,1,3,0,1,15]` |
| SLCG override / HAL flags 47d,47e,484,485 | `0` / `[1,1,0,0]` |

Zero tuning values are measured board inputs, not invented fallback values.
The verification harness executes original parsing and initialization in
Unicorn; its OS/setup callbacks remain stubs. It does not simulate power rails.

## Other acquisition paths and their limits

The optional `CONFIG_N1X_GPU_IOVA_OBSERVE=m` module observes existing GPU
IOMMU translations via the CPU page-table walker. It performs no device bind,
DMA allocation, MMIO/RPC or target-memory read. Nine scratch IOVA pages mapped
to noncontiguous CPU pages. `capture_gpu_iova_frts.py` copies only validated,
reserved RAM pages via `/proc/kcore`, with stable-map and double-copy checks.

The observed 36-KiB GFWI snapshot (SHA256
`b765fae8bfa807e12d3e61aeda198bdd82b04d2c35ab39098cd4067bf0e79256`)
has an offset-2048, length-34816 header but **no BIT/PCIR image** in its stated
payload. A CPU-contiguous extent hypothesis also lacked BIT. Neither is used
as board policy. The evidence does not distinguish overwritten allocation
from stale scratch metadata.

The GSP's original private `0x20800801` handler uses a 16-byte size/pointer
structure and copies a cached ROM. Its zero-size/null-pointer path is replayed
without patching that handler. A physical zero-size probe returned
`NV_ERR_NOT_SUPPORTED` through the Linux host API. No guessed pointer-bearing
request was sent. See `test_gsp_rom_control.py` and the explicit
`--probe-rom-api` option in `read_nvidia_vbios_dump.cpp`.

## Linux prelude

The manual bootstrap now targets the running `npu2` kernel. It maps only the
first 512 bytes of the separately audited HFRP resource `0x31380000`, with
distinct read/write offset allowlists. The DLA/SROOT DMA-domain and machine
guards still apply. Both endpoints' ACPI methods/resources were audited in
[acpi-audit.md](acpi-audit.md).

With explicit `enable_hfrp=1`, the root-only `prepare_hfrp` trigger:

1. Loads `nvidia/n1x/p16-bios300-physical.rom`, checks full size and SHA256,
   parses it and derives gating.
2. Takes runtime-PM ownership and requires observed HFRP stamp `0x20251722`.
3. Pins the module before the first write, performs the bounded handshake,
   discovers the version, sends board-policy startup configuration, then
   sends power request 7.
4. Publishes the completed stage, command, reply bytes and error in
   root-readable `hfrp_status`. Only successful completion enables the
   separate SROOT upload trigger.

No IRQ handler or automatic upload is installed. Unknown asynchronous replies
fail the channel. After the first write attempt, all outcomes retain the
module/runtime references until reboot; no second handshake or reset occurs.
Suspend is rejected while that state is retained. The subsequent host-only
buffer allocations do not publish descriptors until their existing explicit
commands; the Windows additional-setup callback's allocations were separately
replayed in [region5.md](region5.md).

```sh
venv/bin/python scripts/test_linux_hfrp_prepare.py
venv/bin/python scripts/test_linux_bootstrap.py
venv/bin/python scripts/test_linux_bootstrap_guards.py
venv/bin/python scripts/test_hfrp_setup.py
make -C /lib/modules/7.2.8-23-omarchy-p16-asus-npu2/build \
  M="$PWD/driver" n1x_npu_bootstrap.ko
```

The lifecycle test compiles the actual Linux prelude and native ROM parser,
checks the genuine capture/hash, injects each phase failure, and verifies no
later phase or repeat attempt occurs. OS/transport edges are synthetic there;
packet/peer fault tests remain separate. Physical qualification is pending.
In particular the startup response lengths are inferred, IRQ roles remain
unqualified, and no accel API or inference readiness is claimed.
