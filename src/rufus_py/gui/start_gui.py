import subprocess
import json
import sys
import urllib.parse
from pathlib import Path

from rufus_py.drives.find_usb import find_usb

def launch_gui_with_usb_data() -> None:
    usb_devices = find_usb()
    print("Detected USB devices:", usb_devices)

    usb_json = json.dumps(usb_devices)
    encoded_data = urllib.parse.quote(usb_json)

    try:
        gui_path = Path(__file__).resolve().with_name("gui.py")
        subprocess.run([sys.executable, str(gui_path), encoded_data], check=True)
    except FileNotFoundError as e:
        print(f"Failed to launch GUI: executable or script not found: {e}")
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"GUI exited with an error (return code {e.returncode}): {e}")
        sys.exit(e.returncode or e)
    except Exception as e:
        print(f"Unexpected error while launching GUI: {e}")
        sys.exit(1)


if __name__ == "__main__":
    launch_gui_with_usb_data()
