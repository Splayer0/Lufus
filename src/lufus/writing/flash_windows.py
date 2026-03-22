import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from lufus.lufus_logging import get_logger

log = get_logger(__name__)

FAT32_MAX_FILE_SIZE = 4 * 1024 * 1024 * 1024 - 1
WIM_SPLIT_SIZE_MB = 3800
EFI_LABEL = "WINSTALL"


def run(cmd, capture_output: bool = False):
    log.debug("run: %s", cmd)
    return subprocess.run(
        cmd,
        check=True,
        text=True,
        capture_output=capture_output,
    )


def _find_command(*names: str) -> str | None:
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


def _require_command(*names: str) -> str:
    path = _find_command(*names)
    if path:
        return path
    raise FileNotFoundError(f"Required command not found: one of {', '.join(names)}")


def _find_path_case_insensitive(base: str, *parts: str) -> str | None:
    current = base
    for part in parts:
        try:
            entries = os.listdir(current)
        except OSError:
            return None

        match = None
        for entry in entries:
            if entry.lower() == part.lower():
                match = os.path.join(current, entry)
                break

        if match is None:
            return None

        current = match

    return current


def _wait_for_path(path: str, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            return True
        time.sleep(0.2)
    return os.path.exists(path)


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_file_pair(src: str, dst: str, label: str, status_cb=None) -> None:
    src_hash = _sha256(src)
    dst_hash = _sha256(dst)

    msg = f"Verifying {label}: source={src_hash} target={dst_hash}"
    log.info(msg)
    if status_cb:
        status_cb(msg)

    if src_hash != dst_hash:
        raise IOError(f"Verification failed for {label}: {src} != {dst}")


def _copy_file(src: str, dst: str) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)

    with open(src, "rb") as in_fh, open(dst, "wb") as out_fh:
        while True:
            chunk = in_fh.read(8 * 1024 * 1024)
            if not chunk:
                break
            out_fh.write(chunk)

        out_fh.flush()
        os.fsync(out_fh.fileno())

    try:
        shutil.copystat(src, dst, follow_symlinks=False)
    except OSError:
        pass


def _build_copy_manifest(iso_root: str, split_root: str | None):
    manifest = []

    for dirpath, dirnames, filenames in os.walk(iso_root):
        dirnames.sort()
        filenames.sort()

        for filename in filenames:
            src = os.path.join(dirpath, filename)
            rel = os.path.relpath(src, iso_root)
            rel_norm = rel.replace("\\", "/").lower()

            # Skip the original large install image if we split it.
            if split_root and rel_norm in ("sources/install.wim", "sources/install.esd"):
                continue

            size = os.path.getsize(src)
            manifest.append((src, rel, size))

    if split_root:
        for dirpath, dirnames, filenames in os.walk(split_root):
            dirnames.sort()
            filenames.sort()

            for filename in filenames:
                src = os.path.join(dirpath, filename)
                rel = os.path.relpath(src, split_root)
                size = os.path.getsize(src)
                manifest.append((src, rel, size))

    return manifest


def _copy_manifest(
    manifest,
    dst_root: str,
    progress_cb=None,
    base_pct: int = 35,
    span_pct: int = 55,
):
    total_bytes = sum(size for _, _, size in manifest)
    copied = 0
    last_pct = -1

    for src, rel, size in manifest:
        dst = os.path.join(dst_root, rel)
        _copy_file(src, dst)
        copied += size

        if progress_cb and total_bytes > 0:
            pct = base_pct + int((copied / total_bytes) * span_pct)
            pct = min(pct, base_pct + span_pct)
            if pct != last_pct:
                progress_cb(pct)
                last_pct = pct


def _find_install_image(root: str) -> str | None:
    sources_dir = _find_path_case_insensitive(root, "sources")
    if not sources_dir:
        return None

    for name in ("install.wim", "install.esd"):
        candidate = _find_path_case_insensitive(sources_dir, name)
        if candidate:
            return candidate

    return None


def _find_boot_wim(root: str) -> str | None:
    return _find_path_case_insensitive(root, "sources", "boot.wim")


def _prepare_split_install_image(
    install_image: str,
    stage_root: str,
    status_cb=None,
) -> tuple[str, list[str]]:
    wimlib = _require_command("wimlib-imagex")

    stage_sources = os.path.join(stage_root, "sources")
    os.makedirs(stage_sources, exist_ok=True)

    split_base = os.path.join(stage_sources, "install.swm")

    message = (
        f"Splitting {os.path.basename(install_image)} because it exceeds FAT32's 4 GiB limit..."
    )
    log.info(message)
    if status_cb:
        status_cb(message)

    run([wimlib, "split", install_image, split_base, str(WIM_SPLIT_SIZE_MB)])

    split_parts = sorted(str(path) for path in Path(stage_sources).glob("install*.swm"))
    if not split_parts:
        raise FileNotFoundError(
            "wimlib-imagex reported success but no install*.swm files were produced"
        )

    return stage_root, split_parts


def _mount_vfat_partition(device_part: str, mountpoint: str) -> None:
    uid = os.getuid()
    gid = os.getgid()

    run(
        [
            "sudo",
            "mount",
            "-t",
            "vfat",
            "-o",
            f"uid={uid},gid={gid},umask=022,shortname=mixed,utf8=1",
            device_part,
            mountpoint,
        ]
    )


def _mount_iso(iso_path: str, mountpoint: str) -> None:
    run(["sudo", "mount", "-o", "loop,ro", iso_path, mountpoint])


def flash_windows(device: str, iso: str, progress_cb=None, status_cb=None) -> bool:
    if not re.match(r"^/dev/(sd[a-z]+|nvme[0-9]+n[0-9]+|mmcblk[0-9]+)$", device):
        raise ValueError(f"Invalid device path: {device}")

    def _emit(pct: int):
        if progress_cb:
            progress_cb(pct)

    def _status(msg: str):
        log.info(msg)
        if status_cb:
            status_cb(msg)

    _status(
        f"flash_windows: starting UEFI-only Windows media creation for device={device}, iso={iso}"
    )

    try:
        iso_size = os.path.getsize(iso)
    except OSError as e:
        _status(f"flash_windows: cannot read ISO file: {e}")
        return False

    _status(
        f"flash_windows: ISO size = {iso_size:,} bytes ({iso_size / (1024**3):.2f} GiB)"
    )

    try:
        _require_command("sfdisk")
        _require_command("wipefs")
        _require_command("partprobe")
        _require_command("udevadm")
        _require_command("mkfs.vfat")
        _require_command("mount")
        _require_command("umount")
    except FileNotFoundError as e:
        _status(str(e))
        return False

    try:
        with (
            tempfile.TemporaryDirectory(prefix="lufus_iso_") as mount_iso,
            tempfile.TemporaryDirectory(prefix="lufus_usb_") as mount_usb,
            tempfile.TemporaryDirectory(prefix="lufus_stage_") as stage_root,
        ):
            mounted_iso = False
            mounted_usb = False

            try:
                _status(f"Wiping existing partition table on {device}...")
                run(["sudo", "wipefs", "-a", device])
                _emit(8)

                p_prefix = "p" if ("nvme" in device or "mmcblk" in device) else ""
                part1 = f"{device}{p_prefix}1"

                sfdisk_script = f"""label: gpt
device: {device}
unit: sectors

{part1} : start=2048, type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B
"""

                _status(f"Writing GPT with a single FAT32 EFI partition to {device}...")
                subprocess.run(
                    ["sudo", "sfdisk", device],
                    input=sfdisk_script.encode(),
                    check=True,
                )
                run(["sudo", "partprobe", device])
                run(["sudo", "udevadm", "settle"])

                if not _wait_for_path(part1, timeout=12.0):
                    raise FileNotFoundError(f"Partition node did not appear: {part1}")

                _emit(18)

                _status(f"Formatting {part1} as FAT32 ({EFI_LABEL})...")
                run(["sudo", "mkfs.vfat", "-F", "32", "-n", EFI_LABEL, part1])
                _emit(25)

                _status(f"Mounting Windows ISO read-only at {mount_iso}...")
                _mount_iso(iso, mount_iso)
                mounted_iso = True

                _status(f"Mounting USB partition at {mount_usb}...")
                _mount_vfat_partition(part1, mount_usb)
                mounted_usb = True
                _emit(32)

                install_image = _find_install_image(mount_iso)
                if not install_image:
                    raise FileNotFoundError(
                        "Could not find sources/install.wim or sources/install.esd inside the ISO"
                    )

                install_size = os.path.getsize(install_image)
                _status(
                    f"Found install image: {install_image} ({install_size:,} bytes / {install_size / (1024**3):.2f} GiB)"
                )

                split_parts = []
                split_stage_root = None

                if install_size > FAT32_MAX_FILE_SIZE:
                    split_stage_root, split_parts = _prepare_split_install_image(
                        install_image,
                        stage_root,
                        status_cb=status_cb,
                    )
                    _status(f"Split image created into {len(split_parts)} part(s)")
                else:
                    _status("Install image fits inside FAT32; no split needed")

                manifest = _build_copy_manifest(mount_iso, split_stage_root)

                for src, rel, size in manifest:
                    rel_norm = rel.replace("\\", "/").lower()
                    if size > FAT32_MAX_FILE_SIZE and rel_norm not in {
                        "sources/install.wim",
                        "sources/install.esd",
                    }:
                        raise ValueError(
                            f"Cannot place {rel} on FAT32 because it exceeds 4 GiB and only install.wim/install.esd splitting is supported"
                        )

                _status(
                    f"Copying {len(manifest)} file(s) to USB while preserving the original Windows layout..."
                )
                _copy_manifest(
                    manifest,
                    mount_usb,
                    progress_cb=progress_cb,
                    base_pct=35,
                    span_pct=52,
                )
                _emit(88)

                boot_wim_src = _find_boot_wim(mount_iso)
                if boot_wim_src:
                    boot_wim_dst = _find_path_case_insensitive(
                        mount_usb,
                        "sources",
                        "boot.wim",
                    )
                    if not boot_wim_dst:
                        raise FileNotFoundError(
                            "boot.wim was expected on the USB but was not found after copying"
                        )

                    _verify_file_pair(
                        boot_wim_src,
                        boot_wim_dst,
                        "boot.wim",
                        status_cb=status_cb,
                    )

                if split_parts:
                    for split_src in split_parts:
                        rel = os.path.relpath(split_src, split_stage_root)
                        split_dst = os.path.join(mount_usb, rel)
                        if not os.path.exists(split_dst):
                            raise FileNotFoundError(f"Split file missing on USB: {split_dst}")

                        _verify_file_pair(
                            split_src,
                            split_dst,
                            rel,
                            status_cb=status_cb,
                        )
                else:
                    install_dst = os.path.join(
                        mount_usb,
                        os.path.relpath(install_image, mount_iso),
                    )
                    if not os.path.exists(install_dst):
                        raise FileNotFoundError(
                            f"Install image missing on USB: {install_dst}"
                        )

                    _verify_file_pair(
                        install_image,
                        install_dst,
                        os.path.relpath(install_image, mount_iso),
                        status_cb=status_cb,
                    )

                _status("Syncing all writes to disk...")
                run(["sync"])
                _emit(100)

                _status(
                    "flash_windows: finished successfully. This Windows USB is UEFI-only."
                )
                return True

            finally:
                if mounted_usb:
                    subprocess.run(["sudo", "umount", mount_usb], check=False)
                if mounted_iso:
                    subprocess.run(["sudo", "umount", mount_iso], check=False)

    except (OSError, subprocess.CalledProcessError, ValueError, FileNotFoundError) as e:
        log.error("flash_windows: failed: %s", e)
        _status(f"flash_windows: failed: {e}")
        return False
