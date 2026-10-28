#!/usr/bin/env python3
"""Release only orphaned NM DHCP/DNS helpers for the B860H counter networks."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import select
import signal
import subprocess
import sys


NETWORKS = {"wlan1": "10.43.0.1", "eth1": "10.42.0.1"}


def read_process(directory: Path) -> tuple[int, str, tuple[str, ...], str, str]:
    status = directory.joinpath("status").read_text()
    parent = next(int(line.split()[1]) for line in status.splitlines() if line.startswith("PPid:"))
    executable = directory.joinpath("exe").resolve(strict=True).name
    args = tuple(part.decode(errors="surrogateescape")
                 for part in directory.joinpath("cmdline").read_bytes().split(b"\0") if part)
    cgroup = directory.joinpath("cgroup").read_text()
    # Field 22 is the process start time; the comm field may contain spaces.
    started = directory.joinpath("stat").read_text().rsplit(")", 1)[1].split()[19]
    return parent, executable, args, cgroup, started


def matches_orphan(process: tuple[int, str, tuple[str, ...], str, str], interface: str) -> bool:
    parent, executable, args, cgroup, _ = process
    if interface not in NETWORKS:
        return False
    required = {
        "--conf-file=/dev/null", "--keep-in-foreground",
        f"--pid-file=/run/nm-dnsmasq-{interface}.pid",
        f"--listen-address={NETWORKS[interface]}",
        f"--dhcp-leasefile=/var/lib/NetworkManager/dnsmasq-{interface}.leases",
    }
    nm_owned = any(line.split(":", 2)[-1].strip() == "/system.slice/NetworkManager.service"
                   for line in cgroup.splitlines())
    return parent == 1 and executable == "dnsmasq" and nm_owned and required.issubset(args)


def interface_idle(interface: str) -> bool:
    result = subprocess.run(
        ["nmcli", "-g", "GENERAL.STATE", "device", "show", interface],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode:
        return False
    try:
        state = int(result.stdout.split()[0])
    except (ValueError, IndexError):
        return False
    # Never touch a helper for an active or activating interface.
    return state < 40 or state >= 110


def cleanup(interface: str) -> None:
    if not interface_idle(interface):
        return
    for directory in Path("/proc").iterdir():
        if not directory.name.isdigit():
            continue
        try:
            process = read_process(directory)
            if not matches_orphan(process, interface):
                continue
            # Pin the process identity across validation and signalling so a
            # reused PID can never terminate an unrelated process.
            fd = os.pidfd_open(int(directory.name))
            try:
                if read_process(directory) != process or not interface_idle(interface):
                    continue
                signal.pidfd_send_signal(fd, signal.SIGTERM)
                poller = select.poll()
                poller.register(fd, select.POLLIN)
                if not poller.poll(2000):
                    raise RuntimeError(f"Orphaned {interface} dnsmasq PID {directory.name} did not exit")
                print(f"Stopped orphaned NetworkManager dnsmasq for {interface} (PID {directory.name}).")
            finally:
                os.close(fd)
        except (FileNotFoundError, ProcessLookupError):
            continue


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("interfaces", nargs="+", choices=NETWORKS)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("run as root")
    for interface in args.interfaces:
        cleanup(interface)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"B860H DHCP cleanup failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
