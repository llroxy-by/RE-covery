import argparse
import base64
import ipaddress
import subprocess
import tempfile
from pathlib import Path

from console import pinned_key, robot_hostname

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ip", type=ipaddress.IPv4Address, help="IPv4 address shown on the robot")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--reverse-once", action="store_true")
    mode.add_argument("--reverse-5cm", action="store_true")
    mode.add_argument("--read-height", action="store_true")
    mode.add_argument("--raise-5cm", action="store_true")
    args = parser.parse_args()
    if args.ip is None:
        parser.error("--ip is required for SSH")
    ip = str(args.ip)
    payload = Path(__file__).with_name("reverse_remote.py").read_text(encoding="utf-8")
    source = base64.b64encode(payload.replace("G1_HOST_PLACEHOLDER", robot_hostname()).encode()).decode("ascii")
    confirmation = "RAISE 5CM" if args.raise_5cm else "REVERSE 5CM" if args.reverse_5cm else "REVERSE ONCE" if args.reverse_once else ""
    request = "READ HEIGHT" if args.read_height else confirmation
    action = f"printf '{request}\\n' | " if request else ""
    remote = (
        action + "env PYTHONPATH=/data/galbot/lib LD_LIBRARY_PATH=/data/galbot/lib "
        'python3 -B -u -c "import base64; exec(base64.b64decode(\'' + source + '\'))"'
    )
    if confirmation:
        if input(f"Type only {confirmation} to confirm: ") != confirmation:
            raise SystemExit("Cancelled")
    with tempfile.TemporaryDirectory() as directory:
        known = Path(directory) / "known_hosts"
        known.write_text(pinned_key(ip), encoding="ascii")
        result = subprocess.run([
            "ssh", "-n", "-T", "-o", "StrictHostKeyChecking=yes",
            "-o", "UserKnownHostsFile=" + str(known), "-o", "ConnectTimeout=5",
            "galbot@" + ip, remote,
        ])
    raise SystemExit(result.returncode)

if __name__ == "__main__":
    main()

