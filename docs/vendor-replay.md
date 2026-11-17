# Offline vendor-code replay

These optional research tools execute pinned vendor instructions in Unicorn
with private emulator memory. They do not load firmware, access MMIO, bind a
device, install a module or prove that hardware is safe to reset.

Install the research-only dependencies into the repository virtual environment:

```sh
uv pip install --python venv/bin/python -r requirements-research.txt
make research-hfrp
```

The inputs are intentionally not redistributed. Prepare the exact files listed
in [setup](setup.md), then verify them with `scripts/prepare_inputs.py`. Both
replayers independently reject a mismatched SHA-256 before executing code.

## Windows startup path

[`replay_windows_startup.py`](../scripts/replay_windows_startup.py) executes the
pinned Windows 617.42 ARM64 KMD's original outer initializer at RVA `0xd38a88`.
It also executes the original object constructors, HFRP factory wrapper
(`0xd3f6d0`), factory (`0x1964e00`), object initializer (`0xd3d8f0`), transport
constructor (`0xd4aeb8`), callback registration, HFRP wrapper (`0xd41b50`),
mode selector (`0xd3dd08`) and version discovery (`0xd3da30`). OS allocation,
registry access, the transport handshake, policy, mode-1 power and loader
implementations remain explicit boundaries. The original no-transport branch
of the power function (`0xd405f0`) executes for mode 0 and a missing context.

Twenty deterministic cases cover both observed initializer states, injected
failures, HFRP mode 0 and a missing HFRP context. With the synthetic stamp
`0x20251722`, the original version logic records protocol 1.5. The replay shows:

- mode 1 writes resource 2 offset 4 before entering the transport handshake;
- mode 0 deliberately skips that write and the handshake, then its original
  no-transport power branch returns success for the constructed default state;
- a missing HFRP context returns success from the wrapper and follows the same
  original no-transport power branch;
- in the early initializer state, a handshake failure can still be followed by
  power and loader calls and an overall success result;
- in the later state the wrapper is called again and the same failure is then
  propagated.

The original factory allocates a 0x68-byte HFRP object, a 0x40e8-byte transport
and a 0x10-byte link. The link points back to the DLA outer object, and the
original read/write callbacks route HFRP accesses through resource index 2.
This removes the earlier hand-built object graph from the replay, but it does
not identify or start the hardware peer behind that aperture.

Consequently, the replayed Windows outer initializer can report success without
a completed HFRP handshake. That is still not an observed Device Manager or DLA
inference run: the loader and multiple OS/firmware edges are modeled. The result
establishes vendor error-propagation behavior, not the Surface issue's missing
physical prerequisite.

The same hash-pinned scan finds ten signed native envelopes in the KMD. Six
plaintext envelopes collapse to two unique inner images; the other four remain
opaque. Both plaintext families contain the DLA-side `HFRP is busy` path and
power/frequency-management diagnostics. No separate controller/server image is
identified by this inventory. Opaque or encoded payloads prevent treating that
negative result as proof that the peer firmware is absent.

## Firmware client path

[`replay_firmware_hfrp.py`](../scripts/replay_firmware_hfrp.py) accepts only the
optional plaintext `sroot-v1.bin` variant with SHA-256
`6b1e8192fc1e61944e9b2a84251b7d18f305025ffb2cfa1de8677f8f98b59ad4`.
It decodes the signed native envelope and executes the original RV32 function
at VA `0x1060c6`; only logging and critical-section entry are modeled.

The 27 cases show a client-side bandwidth request: all six busy flags return
`-12` without a modeled MMIO write, valid inputs clamp bandwidth to 100--1000,
and successful requests end with value 6 written to private address
`0x01400d40`. A timeout of `0x10000` returns `-3`, but the bandwidth write has
already occurred. An error result is therefore not general proof that this
firmware path left state unchanged.

The request recipient is still unknown. This image is not identified as the
HFRP controller/server, and behavior of this optional plaintext variant cannot
be projected onto the production image. It supplies neither a controller reset
contract nor a DMA-quiescence guarantee.

## Reproducibility boundary

`make check` does not require proprietary inputs, `pefile`, or Unicorn. It runs
the portable C/Python driver tests, including seeded ASan/UBSan property tests
for the HFRP reply parser and the real accelerator IOCTL functions. The vendor
replays are separate so offline CI remains redistributable.
