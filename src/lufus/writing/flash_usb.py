import os
import re
import subprocess
from lufus.writing.check_file_sig import _resolve_device_node
from lufus.writing.check_file_sig import check_iso_signature
from lufus.drives import find_usb as fu
from lufus.drives import states
from lufus.writing.detect_windows import is_windows_iso
from lufus.writing.flash_windows import flash_windows
from lufus.writing.flash_woeusb import flash_woeusb


def pkexecNotFound():
    print(
        "Error: The command pkexec or labeling software was not found on your system."
    )


def FormatFail():
    print("Error: Formatting failed. Was the password correct? Is the drive unmounted?")


def unexpected():
    print("An unexpected error occurred")


def FlashUSB(iso_path, raw_device, progress_cb=None, status_cb=None) -> bool:
    def _status(msg):
        print(msg)
        if status_cb:
            status_cb(msg)

    _status(f"FlashUSB called: iso={iso_path}, device={raw_device}")

    original_device = raw_device
    raw_device = re.sub(r"[0-9]+$", "", raw_device)
    if raw_device != original_device:
        _status(f"Stripped partition suffix: {original_device} -> {raw_device}")

    try:
        iso_size = os.path.getsize(iso_path)
        _status(f"ISO file size: {iso_size:,} bytes ({iso_size / (1024**3):.2f} GiB)")

        _status(f"Validating ISO9660 signature for: {iso_path}")
        if not check_iso_signature(iso_path):
            _status(f"ISO signature check FAILED for {iso_path}, aborting flash")
            return False
        _status("ISO signature check passed")

        _status(f"Checking if ISO contains Windows installation files...")
        if is_windows_iso(iso_path):
            _status("Windows ISO confirmed")
            _status(f"Flash mode state: currentflash={states.currentflash}")

            if states.currentflash == 0:
                _status("Routing to flash_windows (ISO mode)")
                return flash_windows(
                    raw_device,
                    iso_path,
                    progress_cb=progress_cb,
                    status_cb=status_cb,
                )
            elif states.currentflash == 1:
                _status("Routing to flash_woeusb (WoeUSB mode)")
                return flash_woeusb(
                    raw_device,
                    iso_path,
                    progress_cb=progress_cb,
                    status_cb=status_cb,
                )
        else:
            _status("Not a Windows ISO, will use dd for flashing")

        dd_args = [
            "dd",
            f"if={iso_path}",
            f"of={raw_device}",
            "bs=4M",
            "status=progress",
            "conv=fdatasync",
        ]

        _status(f"Spawning dd: {' '.join(dd_args)}")
        _status(
            f"Writing {iso_size:,} bytes to {raw_device}, this may take several minutes..."
        )

        process = subprocess.Popen(
            dd_args, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL
        )
        _status(f"dd process started with PID {process.pid}")

        buf = b""
        last_pct = -1
        while True:
            chunk = process.stderr.read(256)
            if not chunk:
                break
            buf += chunk
            parts = re.split(rb"[\r\n]", buf)
            buf = parts[-1]
            for line in parts[:-1]:
                line = line.strip()
                if not line:
                    continue
                m = re.match(rb"^(\d+)\s+bytes", line)
                if m and iso_size > 0:
                    bytes_done = int(m.group(1))
                    pct = min(int(bytes_done * 100 / iso_size), 99)
                    if pct != last_pct:
                        _status(
                            f"dd progress: {bytes_done:,} / {iso_size:,} bytes ({pct}%)"
                        )
                        last_pct = pct
                    if progress_cb:
                        progress_cb(pct)

        process.wait()
        _status(f"dd process exited with return code {process.returncode}")

        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, dd_args)

        _status(f"dd completed successfully: {iso_path} -> {raw_device}")
        return True

    except subprocess.CalledProcessError as e:
        _status(
            f"Flash failed with CalledProcessError: returncode={e.returncode}, cmd={e.cmd}"
        )
        return False
