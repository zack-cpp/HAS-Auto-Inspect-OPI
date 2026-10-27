from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


pytestmark = pytest.mark.skipif(
    os.name != "posix" or shutil.which("bash") is None,
    reason="B860H AP worker requires Bash",
)
SYNC_SCRIPT = Path(__file__).resolve().parents[1] / "setup" / "b860h-ap-sync.sh"


def run_worker(tmp_path, *, frequency=2437, saved_channel=1, active="ap-uuid", state=100, up_fails=False):
    log = tmp_path / "commands"
    # Mock system interfaces without root or network changes. Run the real
    # synchronizer, so error propagation and restart decisions are exercised.
    harness = r'''
set -Eeuo pipefail
source "$SYNC_SCRIPT"
interfaces_ready() { return 0; }
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
    assert 'wlan0' not in commands


def test_unchanged_channel_does_not_restart_active_ap(tmp_path):
    result, commands = run_worker(tmp_path, saved_channel=6)
    assert result.returncode == 0, result.stderr
    assert 'connection down' not in commands
    assert 'connection up' not in commands
    assert 'connection modify' not in commands


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
