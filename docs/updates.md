# Arch Package and Kernel Updates

## Building and Installing the Package

Build the `n1x-npu-driver` Arch package from the committed `driver/`, `include/`, `runtime/`, and `packaging/` trees:

```sh
python3 scripts/build_arch_module_package.py
sudo pacman -U results/arch-module-package/n1x-npu-driver-*.pkg.tar.*
```

The package installs:
- `/usr/lib/modules/$(uname -r)/updates/n1x-npu-bootstrap/n1x_npu_bootstrap.ko`
- `/usr/lib/udev/rules.d/99-n1x-npu.rules` for accelerator access permissions
- `/usr/bin/n1x-npu-stop`, an explicit checked-stop/unload maintenance command
- `/usr/lib/{libn1x_dla.so,libn1x_compiler.so,libn1x_ort.so}` and `/usr/bin/{n1x-compile,n1x-ort-run}`
- `/usr/include/{n1x_dla.h,n1x_compiler.h,n1x_ort.h,uapi/n1x_dla_accel.h}`
- `/usr/lib/n1x-npu/python/{n1x_dla.py,n1x_compiler.py,n1x_ort.py,loadable.py}`

ACPI modaliases provide normal boot loading. There is no startup service and
no userspace domain-switching rule. See [startup requirements](setup.md#7-normal-module-startup)
for the required translated DMA domain and migration qualification.

## Updating to a New Kernel Release

1. Apply [`patches/arm64-acpi-iort-nvda8600-noncoherent-dma.patch`](../patches/arm64-acpi-iort-nvda8600-noncoherent-dma.patch) (`NVDA8600` `_HRV=1` noncoherent DMA quirk) to the target kernel tree. For passthrough-default kernels, also apply [`arm-smmu-v3-n1x-translated-dma.patch`](../patches/arm-smmu-v3-n1x-translated-dma.patch). Build the kernel and matching headers packages.
2. Rebuild the package against the target kernel release via `KERNEL_RELEASE=<kver> python3 scripts/build_arch_module_package.py`, and run `make check` plus [`scripts/qualify_npu_reload.py`](../scripts/qualify_npu_reload.py).
