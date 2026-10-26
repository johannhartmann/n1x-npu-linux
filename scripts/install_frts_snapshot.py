#!/usr/bin/env python3
"""Deploy one verified capture UKI and append a manual menu entry; no reboot."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess

from build_frts_snapshot import ROOT, PINS, PARAMETER, verify_exports, verify_installed_btf
from build_sroot_kernel import digest
from install_sroot_test import atomic_write
from verify_sroot_uki import verify, RELEASE

TITLE = "N1X FRTS capture"
EFI_NAME = "omarchy_n1x-frts-capture.efi"


def render_entry(before, blake, cmdline):
    if not re.fullmatch("[0-9a-f]{128}", blake):
        raise ValueError("Invalid BLAKE2b digest")
    if any(c in cmdline for c in "\r\n\0") or cmdline.split().count(PARAMETER) != 1:
        raise ValueError("Invalid capture command line")
    if TITLE in before or EFI_NAME in before:
        raise ValueError("Capture entry already exists")
    if re.findall(r"^default_entry:\s*(.*?)\s*$", before, re.M) != ["Omarchy/linux-p16-asus-23"]:
        raise ValueError("Unexpected boot default")
    return before + ("" if before.endswith("\n") else "\n") + (
        f"\n/{TITLE}\ncomment: Read-only early RAM snapshot; select manually\n"
        f"protocol: efi\npath: boot():/EFI/Linux/{EFI_NAME}#{blake}\ncmdline: {cmdline}\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("build", type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0 or platform.release() != RELEASE:
        parser.error("Requires root on the audited npu1 kernel")
    for name, value in (("sys_vendor", "ASUS"), ("product_name", "ProArt P16 H7607BA"),
                        ("bios_version", "H7607BA.300")):
        if (Path("/sys/class/dmi/id") / name).read_text().strip() != value:
            parser.error("Unexpected machine/BIOS")
    build = args.build.resolve(strict=True)
    if not build.is_relative_to(ROOT / "results"):
        parser.error("Expected repository build directory")
    report = json.loads((build / "frts-build.json").read_text())
    if report.get("uki_sha256") == "16ac622a8832ef10a9d1a9da9cfbf2d2301591269245bdb325b0fb96bbc619c1":
        parser.error("This capture UKI hung on the P16; use the capture-OFF diagnostic procedure")
    kernel, uki = build / "kernel", build / "uki"
    image = uki / "linux-p16-asus-23-npu1.efi"
    if report["stage"] != "verified" or report["release"] != RELEASE:
        parser.error("Build is not verified")
    for file, pin in ((image, report["uki_sha256"]),
                      (kernel / "arch/arm64/boot/Image", report["image_sha256"]),
                      (ROOT / "driver/n1x_frts_snapshot.c", report["source_sha256"]),
                      (ROOT / "patches/linux-7.2.8-p16-frts-snapshot.patch", report["patch_sha256"]),
                      (kernel / ".config", PINS[".config"])):
        if digest(file) != pin:
            parser.error("Artifact changed: " + str(file))
    installed = Path("/usr/lib/modules") / RELEASE
    verify_installed_btf(kernel, installed)
    if digest(installed / "build/Module.symvers") != PINS["Module.symvers"]:
        parser.error("Installed module ABI differs")
    exports = verify_exports(kernel, installed / "build/Module.symvers")
    verify(image, kernel / "arch/arm64/boot/Image", uki / "cmdline")
    if subprocess.check_output(["findmnt", "-n", "-o", "FSTYPE", "/boot"], text=True).strip() != "vfat":
        parser.error("Expected mounted FAT EFI partition")
    if shutil.disk_usage("/boot").free < image.stat().st_size + 64 * 1024 * 1024:
        parser.error("Insufficient EFI free space")
    target = Path("/boot/EFI/Linux") / EFI_NAME
    checkpoint = Path("/var/lib/n1x-npu-test/frts-capture")
    with Path("/run/lock/boot-partition.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if target.exists() or checkpoint.exists():
            parser.error("Capture installation already exists; inspect it")
        menu = Path("/boot/limine.conf")
        before = menu.read_bytes()
        if hashlib.sha256(before).hexdigest() != report["boot_menu_sha256"]:
            parser.error("Menu changed since capture UKI build")
        old = re.findall(r"boot\(\):(/EFI/Linux/omarchy_linux-p16-asus-23\.efi)#([a-f0-9]{128})", before.decode())
        if len(old) != 1 or hashlib.blake2b((Path("/boot") / old[0][0].lstrip("/")).read_bytes()).hexdigest() != old[0][1]:
            parser.error("Known-working default Image/hash does not match")
        blake = hashlib.blake2b(image.read_bytes()).hexdigest()
        after = render_entry(before.decode(), blake, (uki / "cmdline").read_text().strip()).encode()
        checkpoint.mkdir(parents=True, mode=0o700)
        (checkpoint / "limine.conf.before").write_bytes(before)
        with target.open("xb") as output, image.open("rb") as source:
            shutil.copyfileobj(source, output)
            output.flush()
            os.fsync(output.fileno())
        if digest(target) != report["uki_sha256"]:
            raise RuntimeError("Deployed UKI hash mismatch; boot menu not changed")
        atomic_write(menu, after)
        if menu.read_bytes() != after:
            raise RuntimeError("Boot menu verification failed")
        (checkpoint / "installed.json").write_text(json.dumps({
            "title": TITLE, "efi": str(target), "sha256": digest(target), "blake2b": blake,
            "matching_kernel_exports": exports, "default_unchanged": True,
            "physical_boot_verified": False}, indent=2) + "\n")
    print(f"Installed manual entry: {TITLE}. Default unchanged. No reboot performed.")


if __name__ == "__main__":
    main()
