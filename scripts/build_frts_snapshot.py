#!/usr/bin/env python3
"""Build a separate, opt-in early-capture Image and UKI; never deploy or reboot.

Keeps the npu1 release/module ABI and reuses its verified initramfs recipe.
The known-working source, installed Image/modules and boot entries are untouched.
"""
import argparse
import json
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile

from build_sroot_kernel import digest
from verify_sroot_uki import verify, RELEASE

ROOT = Path(__file__).resolve().parents[1]
PINS = {
    "arch/arm64/kernel/setup.c": "d0572642224a071b14de5fdd61243d2086016faf3c29765817fd459b503110d6",
    "arch/arm64/kernel/Makefile": "58ed4f860f659dced2e9ea8ccbbc3cb2dfe1ddff936f913618070f16261c0134",
    ".config": "d114b788fbe20195ec6f37b473a7533c04f315033ec3b56b144d0681040fc939",
    "Module.symvers": "de5ed5f7042e29e120a6138288f5b365fd045da754b0831d7379487990df27c6",
    "arch/arm64/boot/Image": "b7814a84b13d146b712555de9e4c6ad341d53895d7147282e20e186a3f22ee32",
}
PARAMETER = "n1x_frts_snapshot=p16-bios300"


def verify_exports(kernel, base_symvers):
    original = {line for line in base_symvers.read_text().splitlines()
                if line.split()[2] == "vmlinux"}
    rebuilt = set((kernel / "vmlinux.symvers").read_text().splitlines())
    if not original or rebuilt != original:
        raise RuntimeError("Rebuilt vmlinux exports/CRCs differ from the installed module ABI")
    return len(rebuilt)


def verify_installed_btf(kernel, installed):
    """A UKI-only replacement must not invalidate the root filesystem's modules."""
    with tempfile.TemporaryDirectory(prefix='n1x-btf-check-') as temporary:
        hashes = []
        for name, image in (('new', kernel / 'vmlinux'), ('installed', installed / 'build/vmlinux')):
            target = Path(temporary) / name
            subprocess.run(['objcopy', '-O', 'binary', '--only-section=.BTF', str(image), str(target)], check=True)
            if not target.stat().st_size:
                raise RuntimeError('Missing BTF metadata in ' + str(image))
            hashes.append(digest(target))
        if hashes[0] != hashes[1]:
            raise RuntimeError('BTF differs from installed modules. A UKI-only update is unsafe; '
                               'package a separate kernel release with matching root modules first.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("base_uki", type=Path, help="Verified npu1 UKI staging directory")
    parser.add_argument("destination", type=Path)
    parser.add_argument("--jobs", type=int, default=8)
    args = parser.parse_args()
    source, base = args.source.resolve(), args.base_uki.resolve()
    dest = args.destination.resolve()
    if platform.release() != RELEASE or not 1 <= args.jobs <= 16:
        parser.error("Requires the audited npu1 kernel and 1..16 build jobs")
    for name, value in (("sys_vendor", "ASUS"), ("product_name", "ProArt P16 H7607BA"),
                        ("bios_version", "H7607BA.300")):
        if (Path("/sys/class/dmi/id") / name).read_text().strip() != value:
            parser.error("Machine/BIOS does not match the candidate")
    if dest.exists() or args.destination.is_symlink() or not dest.is_relative_to(ROOT / "results"):
        parser.error("Destination must be new and under repository results")
    if dest.is_relative_to(source) or dest.is_relative_to(base):
        parser.error("Destination must be outside both source trees")
    for name, pin in PINS.items():
        if digest(source / name) != pin:
            parser.error("Source differs: " + name)
    installed = Path("/usr/lib/modules") / RELEASE
    if digest(installed / "vmlinuz") != PINS["arch/arm64/boot/Image"]:
        parser.error("Installed npu1 Image differs")
    prior = json.loads((base / "uki-build-manifest.json").read_text())
    original = base / "linux-p16-asus-23-npu1.efi"
    if digest(original) != prior["sha256"]:
        parser.error("Base UKI differs from its verification record")
    verify(original, source / "arch/arm64/boot/Image", base / "cmdline")
    command_line = (base / "cmdline").read_text().strip()
    if "n1x_frts_snapshot" in command_line:
        parser.error("Base already contains a snapshot option")
    dest.mkdir()
    kernel, uki = dest / "kernel", dest / "uki"
    # Reflinks or ordinary copies, never shared writable hard links.
    subprocess.run(["cp", "-a", "--reflink=auto", str(source), str(kernel)], check=True)
    patch = ROOT / "patches/linux-7.2.8-p16-frts-snapshot.patch"
    code = ROOT / "driver/n1x_frts_snapshot.c"
    subprocess.run(["patch", "--batch", "--fuzz=0", "-p1", "-i", str(patch)], cwd=kernel, check=True)
    shutil.copy2(code, kernel / "arch/arm64/kernel/n1x_frts_snapshot.c")
    report = {"stage": "building", "release": RELEASE, "deployed": False,
              "base_pins": PINS, "patch_sha256": digest(patch), "source_sha256": digest(code)}
    manifest = dest / "frts-build.json"
    manifest.write_text(json.dumps(report, indent=2) + "\n")
    with (dest / "kernel-build.log").open("w") as log:
        subprocess.run(["make", f"-j{args.jobs}", "Image", "modules"], cwd=kernel,
                       stdout=log, stderr=subprocess.STDOUT, check=True)
    for name in (".config",):
        if digest(kernel / name) != PINS[name]:
            raise RuntimeError("Kernel configuration/exported module ABI changed: " + name)
    exports = verify_exports(kernel, source / "Module.symvers")
    release = subprocess.check_output(["make", "-s", "kernelrelease"], cwd=kernel, text=True).strip()
    if release != RELEASE:
        raise RuntimeError("Unexpected kernel release")
    subprocess.run(["cp", "-a", "--reflink=auto", str(base), str(uki)], check=True)
    # Discard copied build records and output so no stale success can be read.
    for name in ("linux-p16-asus-23-npu1.efi", "uki-build-manifest.json",
                 "limine.conf.proposed", "boot-menu-before.txt", "mkinitcpio.log"):
        (uki / name).unlink(missing_ok=True)
    image = kernel / "arch/arm64/boot/Image"
    shutil.copy2(image, uki / "root/usr/lib/modules" / RELEASE / "vmlinuz")
    # Symbol CRC equality does not cover split BTF type IDs. Rebuild all
    # in-tree modules against this exact vmlinux before packing the initramfs.
    with (dest / 'modules-build.log').open('w') as log:
        subprocess.run(['make', f'-j{args.jobs}', 'modules_install', 'INSTALL_MOD_STRIP=1',
                        'DEPMOD=/bin/true', 'INSTALL_MOD_PATH=' + str(uki / 'root')],
                       cwd=kernel, stdout=log, stderr=subprocess.STDOUT, check=True)
    subprocess.run(['depmod', '-b', str(uki / 'root'), RELEASE], check=True)
    (uki / "cmdline").write_text(command_line + " " + PARAMETER + "\n")
    with (uki / "mkinitcpio.log").open("w") as log:
        subprocess.run(["pkexec", "/usr/bin/python3", str(ROOT / "scripts/run_sroot_mkinitcpio.py"),
                        str(uki)], stdout=log, stderr=subprocess.STDOUT, check=True)
    if "==> ERROR:" in (uki / "mkinitcpio.log").read_text():
        raise RuntimeError("mkinitcpio reported an error")
    output = uki / "linux-p16-asus-23-npu1.efi"
    verification = verify(output, image, uki / "cmdline")
    report.update(stage="verified", image_sha256=digest(image), uki=str(output),
                  uki_sha256=digest(output), verification=verification,
                  matching_kernel_exports=exports,
                  in_tree_modules_rebuilt_against_exact_vmlinux=True,
                  boot_menu_sha256=digest(uki / "boot-menu-before.txt"))
    manifest.write_text(json.dumps(report, indent=2) + "\n")
    print("Verified separate capture UKI; no installation or reboot.")


if __name__ == "__main__":
    main()
