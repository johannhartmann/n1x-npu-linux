# Contributing

## Local Verification

Portable checks require only a Linux host with a C/C++ compiler, `make`, Python 3.12, and Git:

```sh
python3.12 -m venv venv
make check
```

| Change Area | Verification Command |
| --- | --- |
| Documentation, links, JSON files, demo hashes | `make check-repo` |
| Driver protocol, shutdown, `/dev/accel` lifetime | `make test-portable` |
| Kernel module compilation & style | `make module` + `/lib/modules/$(uname -r)/build/scripts/checkpatch.pl` |
| Native `t254` compiler & ONNX EP | `make test-compiler` |
| cuDLA backend & broker | `make test-cudla` |

## Guidelines

- Kernel C code under `driver/` and `include/` follows standard Linux kernel coding style (`checkpatch.pl` clean).
- Cover protocol, `ioctl`, `mmap`, or DMA lifetime changes with offline unit tests in `scripts/test_*.py`.
- Keep vendor binaries, firmware images, ROM captures, generated `.nvda` loadables, and virtual environments out of Git ([LICENSING.md](LICENSING.md), [NOTICE](NOTICE)).
