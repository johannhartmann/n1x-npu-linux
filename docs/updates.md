# Arch Package and Kernel Updates

## Building and Installing the Package

Build the `n1x-npu-driver` Arch package from the committed `driver/`, `include/`, `runtime/`, and `packaging/` trees:

```sh
python3 scripts/build_arch_module_package.py
sudo pacman -U results/arch-module-package/n1x-npu-driver-*.pkg.tar.*
```

The package installs:
- `/usr/lib/modules/$(uname -r)/updates/n1x-npu-bootstrap/n1x_npu_bootstrap.ko`
- `/usr/lib/udev/rules.d/99-n1x-npu.rules` and `/usr/lib/systemd/system/n1x-npu.service`
- `/usr/lib/n1x-npu/stop`, which requests checked shutdown before module removal
- `/usr/lib/{libn1x_dla.so,libn1x_compiler.so,libn1x_ort.so}` and `/usr/bin/{n1x-compile,n1x-ort-run}`
- `/usr/include/{n1x_dla.h,n1x_compiler.h,n1x_ort.h,uapi/n1x_dla_accel.h}`
- `/usr/lib/n1x-npu/python/{n1x_dla.py,n1x_compiler.py,n1x_ort.py,loadable.py}`

Enable automatic startup at boot:

```sh
sudo systemctl enable --now n1x-npu.service
```

## Updating to a New Kernel Release

1. Apply [`patches/arm64-acpi-iort-nvda8600-noncoherent-dma.patch`](../patches/arm64-acpi-iort-nvda8600-noncoherent-dma.patch) (`NVDA8600` `_HRV=1` noncoherent DMA quirk) to the target kernel tree and build the kernel and headers packages.
2. Rebuild the package against the target kernel release via `KERNEL_RELEASE=<kver> python3 scripts/build_arch_module_package.py`, and run `make check` plus [`scripts/qualify_npu_reload.py`](../scripts/qualify_npu_reload.py).
