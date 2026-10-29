# Omarchy and kernel updates

The NPU bootstrap has been physically qualified only on ASUS ProArt P16
H7607BA.300 with `7.2.8-23-omarchy-p16-asus-npu2`. Its code enforces this
board, BIOS and kernel release. The separately named kernel package remains
installed alongside normal `linux-omarchy`; ordinary Omarchy package updates
do not turn a new stock kernel into an NPU kernel.

## Current release

Build the source-pinned module package with matching headers:

```sh
python3 scripts/build_arch_module_package.py
```

The builder exports the committed driver and headers into a fresh directory
under `results/arch-module-package/` and checks the compiled module's kernel
release. Review the resulting PKGBUILD and package, then install the package
file with `sudo pacman -U /absolute/path/to/the-built-package.pkg.tar.zst` in a terminal.
It places only `n1x_npu_bootstrap.ko` in the
versioned module tree and runs `depmod`. It does not autoload the module,
install firmware, start the NPU or change boot entries. Its dependency prevents
removal of the audited kernel while this driver package is installed.

Set the hook's `repo=` variable in `config/omarchy-npu-post-update` to the
quoted absolute path of your checkout. Then install the user-level hook once:

```sh
omarchy hook install post-update config/omarchy-npu-post-update
```

The hook runs the read-only readiness check after Omarchy updates and reports
when the running kernel, board/BIOS, package ownership or loaded module differs.
It also detects an NVIDIA userspace/module version mismatch for the NPU2 kernel.
The external NVIDIA and `spbm` modules under `updates/npu-test/` came from the
isolated test-kernel build; they are not owned by the bootstrap package. A
future NVIDIA update therefore still requires rebuilding and verifying those
modules and the UKI before rebooting into NPU2.
Run `python3 scripts/check_update_readiness.py` at any time. The check cannot
read the root-owned EFI and checkpoint files or prove physical inference.
Verify the current Limine images separately with
`pkexec /usr/bin/python3 "$PWD/scripts/check_update_readiness.py" --boot`.

Keep the known-working `linux-p16-asus-23` and stock Omarchy boot entries.
After an update, verify the selected Limine image and fallback with the
privileged audit appropriate to that entry, then boot the NPU kernel and run
the bounded qualification from [setup](setup.md) before treating it as healthy.
Do not reload the NPU after an uncertain firmware or DMA error; reboot the
same audited kernel first, as documented in [shutdown/reload](shutdown-reload.md).

## Moving to a new kernel

1. Import the new Omarchy kernel recipe into a separate source tree. Preserve
   the existing NPU kernel package, UKI and boot menu entry.
2. Rebase the scoped SROOT noncoherent DMA patch and any required P16 platform
   patches. Inspect the resulting DMA/IOMMU behavior; a clean patch apply or
   successful build is insufficient.
3. Build a new, uniquely named kernel and headers package, external NVIDIA
   modules and an independent UKI. Verify module vermagic, initramfs contents,
   EFI digest, Limine entry and fallback before manual boot.
4. Boot the new kernel without automatically loading the research NPU driver.
   Check input, display, storage, NVIDIA and SROOT DMA on the physical board.
5. Update the driver's explicit kernel gate and package release only after
   review. Rebuild the module and rerun offline regression plus bounded
   physical startup, shutdown, reload and numerical inference checks. Record
   the new evidence before replacing the audited release in the docs.

BIOS updates invalidate the current board policy and ACPI assumptions. Keep
NPU use disabled until firmware tables and DMA behavior are audited again.
