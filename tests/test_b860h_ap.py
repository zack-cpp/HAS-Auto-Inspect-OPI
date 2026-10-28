from __future__ import annotations

import os
import importlib.util
from pathlib import Path
import shutil
import subprocess

import pytest


pytestmark = pytest.mark.skipif(
    os.name != "posix" or shutil.which("bash") is None,
    reason="B860H AP worker requires Bash",
)
SYNC_SCRIPT = Path(__file__).resolve().parents[1] / "setup" / "b860h-ap-sync.sh"
INSTALL_SCRIPT = SYNC_SCRIPT.with_name("setup-kiosk-b860h.sh")
CLEANUP_SCRIPT = SYNC_SCRIPT.with_name("b860h_dnsmasq_cleanup.py")


def run_worker(tmp_path, *, frequency=2437, saved_channel=1, active="ap-uuid", state=100, up_fails=False):
    log = tmp_path / "commands"
    # Mock system interfaces without root or network changes. Run the real
    # synchronizer, so error propagation and restart decisions are exercised.
    harness = r'''
set -Eeuo pipefail
source "$SYNC_SCRIPT"
interfaces_ready() { return 0; }
# Do not touch the host /proc in worker tests. Exercise cleanup policy below.
/usr/local/sbin/counter-inspect-b860h-dnsmasq-cleanup() {
    printf 'cleanup %s\n' "$*" >>"$COMMAND_LOG"
}
nmcli() {
    printf 'nmcli %s\n' "$*" >>"$COMMAND_LOG"
    case "$*" in
        'connection show counter-inspect-b860h-ap') return 0 ;;
        '-g connection.uuid connection show counter-inspect-b860h-ap') echo ap-uuid ;;
        '-g GENERAL.CON-UUID device show wlan1') printf '%s\n' "$ACTIVE_UUID" ;;
        '-g GENERAL.STATE device show wlan1') printf '%s (state)\n' "$AP_STATE" ;;
        '-g 802-11-wireless.channel connection show counter-inspect-b860h-ap') echo "$SAVED_CHANNEL" ;;
        '--wait 60 connection up uuid ap-uuid ifname wlan1') return "$UP_FAILS" ;;
    esac
}
iw() { printf 'freq: %s\n' "$FREQUENCY"; }
iptables() {
    printf 'iptables %s\n' "$*" >>"$COMMAND_LOG"
    [[ "$*" != '-w -C COUNTER_MQTT -i wlan1 -j ACCEPT' ]]
}
sync_ap
'''
    env = dict(os.environ, SYNC_SCRIPT=str(SYNC_SCRIPT), COMMAND_LOG=str(log),
               FREQUENCY=str(frequency), SAVED_CHANNEL=str(saved_channel),
               ACTIVE_UUID=active, AP_STATE=str(state), UP_FAILS=str(int(up_fails)))
    result = subprocess.run(["bash", "-c", harness], env=env, capture_output=True, text=True, timeout=5)
    return result, log.read_text() if log.exists() else ""


def test_channel_change_restarts_only_ap_and_restores_mqtt_rule(tmp_path):
    result, commands = run_worker(tmp_path)
    assert result.returncode == 0, result.stderr
    assert 'connection modify counter-inspect-b860h-ap 802-11-wireless.channel 6' in commands
    assert 'connection down uuid ap-uuid' in commands
    assert 'connection up uuid ap-uuid ifname wlan1' in commands
    assert 'iptables -w -I COUNTER_MQTT 1 -i wlan1 -j ACCEPT' in commands
    assert 'cleanup wlan1' in commands
    assert 'wlan0' not in commands


def test_unchanged_channel_does_not_restart_active_ap(tmp_path):
    result, commands = run_worker(tmp_path, saved_channel=6)
    assert result.returncode == 0, result.stderr
    assert 'connection down' not in commands
    assert 'connection up' not in commands
    assert 'connection modify' not in commands
    assert 'cleanup wlan1' not in commands


def test_offline_upstream_starts_ap_using_saved_channel(tmp_path):
    result, commands = run_worker(tmp_path, frequency="", active="--")
    assert result.returncode == 0, result.stderr
    assert 'connection modify' not in commands
    assert 'connection up uuid ap-uuid ifname wlan1' in commands


def test_unrelated_wlan1_connection_is_preserved(tmp_path):
    result, commands = run_worker(tmp_path, active="operator-connection")
    assert result.returncode != 0
    assert 'unrelated connection' in result.stderr
    assert 'connection down' not in commands
    assert 'connection modify' not in commands


def test_ap_activation_failure_is_reported_without_touching_upstream(tmp_path):
    result, commands = run_worker(tmp_path, active="--", up_fails=True)
    assert result.returncode != 0
    assert 'wlan0' not in commands
    assert 'GENERAL.STATE,GENERAL.REASON' in commands
    assert 'journalctl' in result.stderr


def test_in_progress_activation_is_not_restarted(tmp_path):
    result, commands = run_worker(tmp_path, state=50)
    assert result.returncode == 0, result.stderr
    assert 'still in progress' in result.stdout
    assert 'connection up' not in commands
    assert 'connection down' not in commands
    assert 'connection modify' not in commands


def test_failed_state_with_selected_uuid_is_retried(tmp_path):
    result, commands = run_worker(tmp_path, state=120, saved_channel=6)
    assert result.returncode == 0, result.stderr
    assert 'connection up uuid ap-uuid ifname wlan1' in commands


def run_installer_functions(body, tmp_path):
    env = dict(os.environ, INSTALL_SCRIPT=str(INSTALL_SCRIPT),
               ATTEMPT_FILE=str(tmp_path / "attempt"))
    return subprocess.run(
        ["bash", "-c", 'set -Eeuo pipefail\nsource "$INSTALL_SCRIPT"\n' + body],
        env=env, capture_output=True, text=True, timeout=5,
    )


def test_rerun_finds_existing_ap_with_different_keyfile_name(tmp_path):
    result = run_installer_functions(r'''
nmcli() { printf '%s\n' 'upstream-uuid:factory-wifi' 'ap-uuid:counter-inspect-b860h-ap'; }
find_profile_file() { printf '/etc/NetworkManager/system-connections/renamed-ap.nmconnection\n'; }
resolve_ap_profile
printf 'uuid=%s\nfile=%s\n' "$existing_uuid" "$PROFILE_FILE"
''', tmp_path)
    assert result.returncode == 0, result.stderr
    assert 'uuid=ap-uuid' in result.stdout
    assert 'file=/etc/NetworkManager/system-connections/renamed-ap.nmconnection' in result.stdout


def test_duplicate_ap_names_fail_without_replacing_credentials(tmp_path):
    result = run_installer_functions(r'''
nmcli() { printf '%s\n' 'uuid-1:counter-inspect-b860h-ap' 'uuid-2:counter-inspect-b860h-ap'; }
resolve_ap_profile
''', tmp_path)
    assert result.returncode != 0
    assert 'Multiple' in result.stderr


def test_dbus_disconnect_retries_after_daemon_recovers(tmp_path):
    result = run_installer_functions(r'''
nmcli() {
    if [[ "$*" == '-t -f RUNNING general' ]]; then echo running; return 0; fi
    if [[ ! -f "$ATTEMPT_FILE" ]]; then
        touch "$ATTEMPT_FILE"
        echo 'Error: Message recipient disconnected from message bus without replying' >&2
        return 10
    fi
    echo 'Profile updated'
}
nmcli_retry connection modify uuid ap-uuid 802-11-wireless-security.pmf disable
''', tmp_path)
    assert result.returncode == 0, result.stderr
    assert 'Profile updated' in result.stdout
    assert 'one retry' in result.stderr


def test_profile_lookup_does_not_treat_daemon_failure_as_missing_credentials(tmp_path):
    result = run_installer_functions(r'''
nmcli() { echo 'Error: D-Bus communication failed' >&2; return 10; }
resolve_ap_profile
echo 'incorrectly proceeding to credential prompts'
''', tmp_path)
    assert result.returncode != 0
    assert 'incorrectly proceeding' not in result.stdout
    assert 'D-Bus communication failed' in result.stderr


def load_cleanup_module():
    spec = importlib.util.spec_from_file_location("b860h_cleanup", CLEANUP_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def orphan_metadata():
    return (
        1, "dnsmasq",
        ("/usr/sbin/dnsmasq", "--conf-file=/dev/null", "--keep-in-foreground",
         "--pid-file=/run/nm-dnsmasq-wlan1.pid", "--listen-address=10.43.0.1",
         "--dhcp-leasefile=/var/lib/NetworkManager/dnsmasq-wlan1.leases"),
        "0::/system.slice/NetworkManager.service\n", "123456",
    )


def test_only_verified_nm_orphan_matches_cleanup_policy():
    cleanup = load_cleanup_module()
    orphan = orphan_metadata()
    assert cleanup.matches_orphan(orphan, "wlan1")
    # Active NM child, unrelated service, different subnet, and another
    # interface must survive, even if the executable happens to be dnsmasq.
    assert not cleanup.matches_orphan((9440, *orphan[1:]), "wlan1")
    assert not cleanup.matches_orphan((*orphan[:3], "0::/system.slice/dnsmasq.service", orphan[4]), "wlan1")
    wrong_args = tuple(arg.replace("10.43.0.1", "192.168.50.1") for arg in orphan[2])
    assert not cleanup.matches_orphan((1, "dnsmasq", wrong_args, *orphan[3:]), "wlan1")
    assert not cleanup.matches_orphan(orphan, "eth1")
    assert not cleanup.matches_orphan(orphan, "wlan0")


@pytest.mark.parametrize("state,expected", [(30, True), (50, False), (100, False), (120, True)])
def test_cleanup_skips_active_and_activating_interfaces(monkeypatch, state, expected):
    cleanup = load_cleanup_module()
    monkeypatch.setattr(cleanup.subprocess, "run", lambda *_args, **_kwargs:
                        subprocess.CompletedProcess([], 0, f"{state} (state)\n", ""))
    assert cleanup.interface_idle("wlan1") is expected
