import subprocess


def is_windows_iso(iso_path: str) -> bool:
    print(f"Windows detection: checking {iso_path}")

    try:
        print("Windows detection: running 7z to list ISO contents...")
        result = subprocess.run(
            ["7z", "l", iso_path], capture_output=True, text=True, timeout=30
        )
        print(f"Windows detection: 7z exited with code {result.returncode}")
        if result.returncode == 0:
            files = result.stdout.lower()
            markers = [
                "sources/install.wim",
                "sources/install.esd",
                "sources\\install.wim",
                "sources\\install.esd",
            ]
            for marker in markers:
                if marker in files:
                    print(
                        f"Windows detection: found marker '{marker}' in 7z listing -> Windows ISO confirmed"
                    )
                    return True
            print("Windows detection: none of the Windows markers found in 7z listing")
        else:
            print(f"Windows detection: 7z stderr: {result.stderr.strip()[:200]}")
    except FileNotFoundError:
        print(
            "Windows detection: 7z not found - install p7zip-full: sudo apt install p7zip-full"
        )
    except subprocess.TimeoutExpired:
        print("Windows detection: 7z timed out listing ISO after 30s")
    except Exception as e:
        print(f"Windows detection: 7z unexpected error: {type(e).__name__}: {e}")

    print("Windows detection: falling back to blkid volume label check...")
    try:
        result = subprocess.run(
            ["sudo", "blkid", "-o", "value", "-s", "LABEL", iso_path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        label = result.stdout.strip().upper()
        print(
            f"Windows detection: blkid returned label={label!r} (exit code {result.returncode})"
        )
        if "WIN" in label or "WINDOWS" in label:
            print(
                f"Windows detection: Windows label match -> Windows ISO confirmed via label"
            )
            return True
        print("Windows detection: label does not match Windows patterns")
    except Exception as e:
        print(f"Windows detection: blkid error: {type(e).__name__}: {e}")

    print("Windows detection: result -> NOT a Windows ISO")
    return False
