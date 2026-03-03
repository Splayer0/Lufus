import psutil
from pathlib import Path
import os

def check_iso_signature(file_path: str) -> bool:
    """
    Validate ISO9660 Primary Volume Descriptor at sector 16.
    Offsets:
      32768: volume descriptor type (0x01 for PVD)
      32769-32773: standard identifier 'CD001'
      32774: version (0x01)
    """
    p = Path(file_path)
    if not p.is_file():
        print(f"Error: {file_path} is not a valid file.")
        return False

    try:
        with p.open("rb") as f:
            f.seek(32768)
            data = f.read(7)
            if len(data) < 7:
                print(f"Error: {file_path} is too small to contain a valid PVD.")
                return False
            
            vd_type, ident, version = data[0], data[1:6], data[6]
            if vd_type == 0x01 and ident == b"CD001" and version == 0x01:
                print(f"Valid ISO file: {file_path}")
                return True
            
            else:
                print(f"Error: {file_path} does not have a valid ISO9660 PVD signature.")
                return False
    except OSError as err:
        print(f"Error reading {file_path}: {err}")
        # no need to return here since we will return False at the end
        # log err instead, same thing for Exception
    except Exception as err:
        print(f"Unexpected error: {err}")
    

    return False

def _parent_block_device(device_node: str) -> str | None:
    dev_name = os.path.basename(device_node)
    sys_class = Path("/sys/class/block") / dev_name

    try:
        parent_name = sys_class.resolve().parent.name
        if parent_name == dev_name:
            # alr whole disk device
            return device_node
        return f"/dev/{parent_name}"
    except OSError:
        return None


def _is_removable_device(device_node: str) -> bool:
    """Check that a device is removable (e.g. USB stick) before writing to it."""
    disk_node = _parent_block_device(device_node=device_node) or device_node
    base_name = os.path.basename(disk_node) # no rstrip() func here; breaks device names like mmcblk0
    removable_path = Path("/sys/block") / base_name / "removable"

    try:
        return removable_path.read_text().strip() == "1"
    except OSError:
        return False


def _resolve_device_node(usb_mount_path: str) -> str | None:
    """Resolve a mount path to its underlying device node for dd."""
    normalized = os.path.normpath(usb_mount_path)
    for part in psutil.disk_partitions(all=True):
        if os.path.normpath(part.mountpoint) == normalized:
            return _parent_block_device(part.device) or part.device
    return None