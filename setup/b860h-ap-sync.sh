#!/usr/bin/env bash
# Installed as /usr/local/sbin/counter-inspect-b860h-ap-sync.
set -Eeuo pipefail

readonly PROFILE="counter-inspect-b860h-ap"
readonly CLIENT_IFACE="wlan0"
readonly AP_IFACE="wlan1"

interfaces_ready() {
    [[ -d /sys/class/net/$CLIENT_IFACE && -d /sys/class/net/$AP_IFACE ]]
}

sync_ap() {
    local channel configured_channel active_uuid profile_uuid state activation_rc

    interfaces_ready || {
        echo "B860H Wi-Fi interfaces are not ready; retrying on the next timer tick."
        return 0
    }
    nmcli connection show "$PROFILE" >/dev/null
    profile_uuid="$(nmcli -g connection.uuid connection show "$PROFILE")"
    active_uuid="$(nmcli -g GENERAL.CON-UUID device show "$AP_IFACE")"
    if [[ -n "$active_uuid" && "$active_uuid" != "--" && "$active_uuid" != "$profile_uuid" ]]; then
        echo "Refusing to replace an unrelated connection on $AP_IFACE." >&2
        return 1
    fi
    state="$(nmcli -g GENERAL.STATE device show "$AP_IFACE")"
    state="${state%% *}"
    if [[ "$active_uuid" == "$profile_uuid" ]]; then
        if [[ "$state" =~ ^[0-9]+$ ]] && (( state >= 40 && state < 100 )); then
            echo "AP activation is still in progress; leaving NetworkManager to complete it."
            return 0
        elif [[ "$state" != 100 ]]; then
            # Having our UUID selected is not proof the AP is connected.
            active_uuid=""
        fi
    fi

    # Realtek's two netdevs share one radio. Pin the AP to the associated
    # upstream channel; leave its saved channel alone while upstream is down.
    channel="$(iw dev "$CLIENT_IFACE" link | awk '
        $1 == "freq:" {
            if ($2 >= 2412 && $2 <= 2472) print int(($2 - 2407) / 5)
        }
    ')"
    configured_channel="$(nmcli -g 802-11-wireless.channel connection show "$PROFILE")"
    if [[ -n "$channel" && "$channel" != "$configured_channel" ]]; then
        nmcli connection modify "$PROFILE" 802-11-wireless.channel "$channel"
        # Restart only our AP. Never disconnect or reconfigure wlan0.
        if [[ "$active_uuid" == "$profile_uuid" ]]; then
            nmcli --wait 15 connection down uuid "$profile_uuid"
            active_uuid=""
        fi
        echo "Aligned B860H AP with upstream channel $channel."
    fi

    # Reinsert after any base-installer/firewall restart. Do not open MQTT on
    # the upstream interface. The base chain's final reject stays in place.
    if iptables -w -n -L COUNTER_MQTT >/dev/null 2>&1; then
        if ! iptables -w -C COUNTER_MQTT -i "$AP_IFACE" -j ACCEPT 2>/dev/null; then
            iptables -w -I COUNTER_MQTT 1 -i "$AP_IFACE" -j ACCEPT
        fi
    else
        echo "Counter MQTT firewall chain is missing; AP activation deferred." >&2
        return 1
    fi
    if [[ "$active_uuid" != "$profile_uuid" ]]; then
        # A crashed NetworkManager with KillMode=process may leave its old
        # dnsmasq behind, holding 10.43.0.1:53. Only reap verified orphans.
        /usr/local/sbin/counter-inspect-b860h-dnsmasq-cleanup wlan1
        if nmcli --wait 60 connection up uuid "$profile_uuid" ifname "$AP_IFACE"; then
            echo "B860H AP connection activated."
        else
            activation_rc=$?
            nmcli -f GENERAL.STATE,GENERAL.REASON device show "$AP_IFACE" >&2 || true
            echo "AP activation failed. Check: journalctl -b -u NetworkManager -u wpa_supplicant -n 100 --no-pager" >&2
            return "$activation_rc"
        fi
    fi
}

# One worker at a time, including manual operator invocations.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    exec 9>/run/lock/counter-inspect-b860h-ap.lock
    flock -n 9 || exit 0
    sync_ap
fi
