import subprocess
import os
import glob
import tempfile
import re
from typing import Optional, Callable


def run(cmd):
    subprocess.run(cmd, check=True)


def run_out(cmd) -> str:
    return subprocess.check_output(cmd, text=True).strip()


def _get_wim_size(data_mount) -> int:
    for pattern in ["install.wim", "install.esd", "INSTALL.WIM", "INSTALL.ESD"]:
        matches = glob.glob(f"{data_mount}/sources/{pattern}")
        if matches:
            size = os.path.getsize(matches[0])
            print(f"Found {matches[0]} ({size:,} bytes / {size / (1024**3):.2f} GiB)")
            return size
    print("WARNING: install.wim/install.esd not found in data partition sources/")
    return 0


def _find_path_case_insensitive(base, *parts):
    current = [base]
    for part in parts:
        next_level = []
        for c in current:
            next_level += [
                p
                for p in glob.glob(os.path.join(c, "*"))
                if os.path.basename(p).lower() == part.lower()
            ]
        current = next_level
    result = current[0] if current else None
    return result


def _fix_efi_bootloader(efi_mount):
    """
    Ensure /EFI/BOOT/BOOTX64.EFI exists - required by UEFI spec.
    Windows ISOs put the bootloader at efi/microsoft/boot/efisys.bin
    but UEFI firmware looks for /EFI/BOOT/BOOTX64.EFI as fallback.
    """
    print(f"EFI bootloader fix: checking {efi_mount}")
    found_boot_dir = _find_path_case_insensitive(efi_mount, "EFI", "BOOT")
    boot_dir = found_boot_dir or os.path.join(efi_mount, "EFI", "BOOT")
    existing_bootx64 = _find_path_case_insensitive(
        efi_mount, "EFI", "BOOT", "BOOTX64.EFI"
    )
    if existing_bootx64:
        print(f"EFI bootloader fix: BOOTX64.EFI already present at {existing_bootx64}")
        return

    print(
        f"EFI bootloader fix: BOOTX64.EFI not found, will attempt to create at {boot_dir}"
    )
    bootx64 = os.path.join(boot_dir, "BOOTX64.EFI")
    run(["sudo", "mkdir", "-p", boot_dir])
    print(f"EFI bootloader fix: created directory {boot_dir}")

    src = _find_path_case_insensitive(
        efi_mount, "EFI", "Microsoft", "Boot", "bootmgfw.efi"
    )
    if src:
        run(["sudo", "cp", src, bootx64])
        print(f"EFI bootloader fix: copied {src} -> {bootx64}")
        return

    print(
        "EFI bootloader fix: WARNING - could not find bootmgfw.efi, UEFI boot may fail"
    )


def flash_windows(device, iso, progress_cb=None, status_cb=None):
    if not re.match(r"^/dev/(sd[a-z]|nvme[0-9]n[0-9])$", device):
        raise ValueError(f"Invalid device path: {device}")

    def _emit(pct):
        if progress_cb:
            progress_cb(pct)

    def _status(msg):
        print(msg)
        if status_cb:
            status_cb(msg)

    _status(f"flash_windows: starting for device={device}, iso={iso}")

    iso_size = os.path.getsize(iso)
    _status(
        f"flash_windows: ISO size = {iso_size:,} bytes ({iso_size / (1024**3):.2f} GiB)"
    )

    with (
        tempfile.TemporaryDirectory() as mount_efi,
        tempfile.TemporaryDirectory() as mount_data,
        tempfile.TemporaryDirectory() as host_extract,
    ):

        _status(
            f"flash_windows: temp dirs -> EFI mount={mount_efi}, data mount={mount_data}, extract={host_extract}"
        )

        _status(f"Wiping existing partition table on {device}...")
        run(["sudo", "wipefs", "-a", device])
        _emit(8)

        sfdisk_script = f"""label: gpt
device: {device}
{device}1 : size=512M, type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B
{device}2 : type=EBD0A0A2-B9E5-4433-87C0-68B6B72699C7
"""
        _status(
            f"Writing GPT partition table to {device}: 512MiB EFI (FAT32) + remainder data (NTFS)..."
        )
        subprocess.run(
            ["sudo", "sfdisk", device], input=sfdisk_script.encode(), check=True
        )
        run(["sudo", "partprobe", device])
        _status("partprobe notified kernel of new partition table")
        run(["sudo", "udevadm", "settle"])
        _status("udevadm settled")
        _emit(15)

        efi = f"{device}1"
        data = f"{device}2"
        _status(f"Partitions: EFI={efi}, data={data}")

        _status(f"Formatting {efi} as FAT32 with label BOOT...")
        run(["sudo", "mkfs.vfat", "-F32", "-n", "BOOT", efi])
        _status(f"Formatting {data} as NTFS with label WINDOWS...")
        run(["sudo", "mkfs.ntfs", "-f", "-L", "WINDOWS", data])
        _emit(22)

        _status(f"Mounting {efi} -> {mount_efi}")
        run(["sudo", "mount", efi, mount_efi])
        _status(f"Mounting {data} -> {mount_data}")
        run(["sudo", "mount", data, mount_data])

        try:
            _status(f"Extracting ISO {iso} to {host_extract} with 7z...")
            run(["7z", "x", iso, f"-o{host_extract}", "-y"])
            extracted = os.listdir(host_extract)
            _status(
                f"Extraction complete: {len(extracted)} top-level items: {extracted}"
            )
            _emit(60)

            _status(f"Copying {len(extracted)} items to data partition {mount_data}...")
            items = [os.path.join(host_extract, i) for i in extracted]
            run(["sudo", "cp", "-r"] + items + [mount_data])
            _emit(75)

            wim_size = _get_wim_size(mount_data)
            _status(
                f"install.wim/esd on data partition: {wim_size / (1024**3):.2f} GiB"
            )

            _status("Copying EFI boot files to EFI partition...")
            efi_src = _find_path_case_insensitive(host_extract, "EFI")
            if efi_src:
                efi_items = os.listdir(efi_src)
                _status(
                    f"Found EFI/ directory with {len(efi_items)} items: {efi_items}"
                )
                run(
                    ["sudo", "cp", "-r"]
                    + [os.path.join(efi_src, i) for i in efi_items]
                    + [mount_efi]
                )
                _status("Copied EFI/ tree to EFI partition")
            else:
                _status(
                    "WARNING: No EFI directory found in ISO - drive may not be UEFI bootable"
                )

            boot_src = _find_path_case_insensitive(host_extract, "boot")
            if boot_src:
                boot_items = os.listdir(boot_src)
                _status(f"Found boot/ directory with {len(boot_items)} items")
                run(
                    ["sudo", "cp", "-r"]
                    + [os.path.join(boot_src, i) for i in boot_items]
                    + [mount_efi]
                )
                _status("Copied boot/ tree to EFI partition")
            else:
                _status("No boot/ directory found in ISO extract")

            for f in ["bootmgr", "bootmgr.efi"]:
                src = _find_path_case_insensitive(host_extract, f)
                if src:
                    run(["sudo", "cp", src, f"{mount_efi}/{f}"])
                    _status(f"Copied {f} to EFI partition root")
                else:
                    _status(f"{f} not found in ISO extract (may be fine)")

            _fix_efi_bootloader(mount_efi)
            _emit(88)

            _status("Syncing all writes to disk (this may take a moment)...")
            run(["sudo", "sync"])
            _emit(97)
            _status("Sync complete")
        finally:
            _status(f"Unmounting {mount_efi} and {mount_data}...")
            run(["sudo", "umount", mount_efi])
            run(["sudo", "umount", mount_data])
            _status("Unmount complete")

        _status("flash_windows: finished successfully, Windows USB is ready")
        return True
