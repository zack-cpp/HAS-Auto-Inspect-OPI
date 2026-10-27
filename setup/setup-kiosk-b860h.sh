#!/usr/bin/env bash
set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly BASE_INSTALLER="$SCRIPT_DIR/setup-kiosk.sh"
readonly SYNC_SOURCE="$SCRIPT_DIR/b860h-ap-sync.sh"
readonly SYNC_TARGET="/usr/local/sbin/counter-inspect-b860h-ap-sync"
readonly PROFILE="counter-inspect-b860h-ap"
readonly PROFILE_FILE="/etc/NetworkManager/system-connections/$PROFILE.nmconnection"
readonly AP_ADDRESS="10.43.0.1/24"
readonly SOURCE_APP_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
WIFI_ONLY=false
CONFIGURE_AP=false
KIOSK_URL=""
TEMP_PROFILE=""

usage() {
    cat <<EOF
Usage: sudo bash $0 [--wifi-only] [--configure-ap] [URL]

ZTE B860H with the concurrent RTL8189FS driver only:
  wlan0: existing upstream Wi-Fi client (preserved)
  wlan1: WPA2 hotspot, $AP_ADDRESS, DHCP/NAT via NetworkManager

Default: run the shared kiosk installer, then configure the hotspot.
--wifi-only: add/update the hotspot on an already installed kiosk.
--configure-ap: prompt for a new SSID and password even if a profile exists.
Existing AP credentials are otherwise preserved. Passwords are hidden prompts.
The upstream connection must already be configured; this script does not switch it.
EOF
}

die() { echo "Error: $*" >&2; exit 1; }

cleanup() {
    if [[ -n "$TEMP_PROFILE" ]]; then
        rm -f -- "$TEMP_PROFILE"
    fi
    unset ap_password ap_confirmation
}
trap cleanup EXIT

while (( $# )); do
    case "$1" in
        --wifi-only) WIFI_ONLY=true ;;
        --configure-ap) CONFIGURE_AP=true ;;
        -h|--help) usage; exit 0 ;;
        --*) die "Unknown option: $1" ;;
        *) [[ -z "$KIOSK_URL" ]] || die "Only one URL is accepted."; KIOSK_URL="$1" ;;
    esac
    shift
done

(( EUID == 0 )) || die "Run this installer with sudo."
model="$(tr -d '\0' </proc/device-tree/model)"
[[ "$model" == *B860H* ]] || die "This installer is only for ZTE B860H, detected '$model'."
[[ -r "$BASE_INSTALLER" && -r "$SYNC_SOURCE" ]] || die "Copy the complete setup directory."
for command in iw nmcli readlink; do
    command -v "$command" >/dev/null || die "Install $command before setting up B860H Wi-Fi."
done
for iface in wlan0 wlan1; do
    [[ -d /sys/class/net/$iface/device ]] || die "$iface missing; install the B860H RTL8189FS driver first."
    [[ "$(basename "$(readlink -f "/sys/class/net/$iface/device/driver")")" == rtl8189fs ]] ||
        die "$iface is not driven by rtl8189fs."
done
[[ "$(readlink -f /sys/class/net/wlan0/device)" == "$(readlink -f /sys/class/net/wlan1/device)" ]] ||
    die "wlan0 and wlan1 must belong to the same RTL8189FS SDIO radio."
[[ "$(cat /sys/class/net/wlan0/address)" != "$(cat /sys/class/net/wlan1/address)" ]] ||
    die "The two driver interfaces have identical MAC addresses."
grep -qx CONFIG_CONCURRENT_MODE /proc/net/rtl8189fs/drv_cfg ||
    die "The installed driver does not report CONFIG_CONCURRENT_MODE."
[[ "$(nmcli -g WIFI-PROPERTIES.AP device show wlan1)" == yes ]] || die "wlan1 does not support AP mode."
active_uuid="$(nmcli -g GENERAL.CON-UUID device show wlan1)"
existing_uuid="$(nmcli -g connection.uuid connection show "$PROFILE" 2>/dev/null || true)"
if [[ -n "$active_uuid" && "$active_uuid" != "--" && "$active_uuid" != "$existing_uuid" ]]; then
    die "wlan1 has an unrelated active connection; stop it explicitly before running setup."
fi

if ! $WIFI_ONLY; then
    if [[ -d "$SOURCE_APP_DIR/.git" && -r "$SOURCE_APP_DIR/compose.yaml" ]]; then
        export COUNTER_APP_DIR="$SOURCE_APP_DIR"
    fi
    if [[ -z "$KIOSK_URL" && -r /etc/kiosk/url ]]; then
        KIOSK_URL="$(tr -d '\r\n' </etc/kiosk/url)"
    fi
    if [[ -n "$KIOSK_URL" ]]; then
        bash "$BASE_INSTALLER" "$KIOSK_URL"
    else
        bash "$BASE_INSTALLER"
    fi
elif [[ -n "$KIOSK_URL" ]]; then
    die "A kiosk URL is not used with --wifi-only."
fi
[[ -x /usr/local/sbin/counter-inspect-mqtt-firewall ]] ||
    die "Install the kiosk first or rerun without --wifi-only."

apt-get install -y dnsmasq-base python3 iw

# Validate against upstream, wired-counter, and Docker routes before assigning
# a separate subnet. Ignore our own pre-existing AP route on repeat installs.
python3 - <<'PY'
import ipaddress
import json
import subprocess
ap = ipaddress.ip_network("10.43.0.0/24")
routes = json.loads(subprocess.check_output(["ip", "-j", "-4", "route", "show", "table", "all"]))
for route in routes:
    destination = route.get("dst", "default")
    if destination == "default" or route.get("dev") == "wlan1":
        continue
    if ap.overlaps(ipaddress.ip_network(destination, strict=False)):
        raise SystemExit(f"AP subnet {ap} conflicts with {destination} on {route.get('dev')}")
PY

install -d -o root -g root -m 0700 /etc/NetworkManager/system-connections
if [[ ! -f "$PROFILE_FILE" ]] || $CONFIGURE_AP; then
    # Keep credentials out of process arguments, environment variables and logs.
    # Escape NetworkManager keyfile strings (including leading/trailing spaces).
    keyfile_escape() {
        local value="$1"
        value="${value//\\/\\\\}"
        value="${value// /\\s}"
        printf '%s' "$value"
    }
    default_ssid="HAS-AP-$(hostname -s)"
    default_ssid="${default_ssid:0:32}"
    read -r -p "AP SSID [$default_ssid]: " ap_ssid
    ap_ssid="${ap_ssid:-$default_ssid}"
    # Restrict to printable ASCII to make the 32-byte SSID limit unambiguous.
    LC_ALL=C
    [[ ${#ap_ssid} -le 32 && "$ap_ssid" != *[^[:print:]]* ]] || die "SSID must be 1-32 printable ASCII characters."
    read -r -s -p "AP password (8-63 printable ASCII characters): " ap_password
    printf '\n'
    read -r -s -p "Confirm AP password: " ap_confirmation
    printf '\n'
    [[ "$ap_password" == "$ap_confirmation" ]] || die "Passwords do not match."
    [[ ${#ap_password} -ge 8 && ${#ap_password} -le 63 && "$ap_password" != *[^[:print:]]* ]] ||
        die "Password must be 8-63 printable ASCII characters."
    profile_uuid="${existing_uuid:-$(cat /proc/sys/kernel/random/uuid)}"
    channel="$(iw dev wlan0 link | awk '$1 == "freq:" && $2 >= 2412 && $2 <= 2472 { print int(($2 - 2407) / 5) }')"
    TEMP_PROFILE="$(mktemp /etc/NetworkManager/system-connections/.b860h-ap.XXXXXX)"
    chmod 0600 "$TEMP_PROFILE"
    {
        printf '[connection]\nid=%s\nuuid=%s\ntype=wifi\ninterface-name=wlan1\nautoconnect=false\n\n' "$PROFILE" "$profile_uuid"
        printf '[wifi]\nmode=ap\nband=bg\nchannel=%s\nssid=%s\npowersave=2\n\n' "${channel:-6}" "$(keyfile_escape "$ap_ssid")"
        printf '[wifi-security]\nkey-mgmt=wpa-psk\nproto=rsn;\npairwise=ccmp;\ngroup=ccmp;\npsk=%s\n\n' "$(keyfile_escape "$ap_password")"
        printf '[ipv4]\nmethod=shared\naddress1=%s\nnever-default=true\n\n[ipv6]\nmethod=disabled\n' "$AP_ADDRESS"
    } >"$TEMP_PROFILE"
    mv -f "$TEMP_PROFILE" "$PROFILE_FILE"
    TEMP_PROFILE=""
    unset ap_password ap_confirmation
else
    echo "Preserving existing hotspot SSID and credentials."
fi
chmod 0600 "$PROFILE_FILE"
chown root:root "$PROFILE_FILE"
nmcli connection load "$PROFILE_FILE"

install -o root -g root -m 0755 "$SYNC_SOURCE" "$SYNC_TARGET"
cat >/etc/systemd/system/counter-inspect-b860h-ap.service <<'EOF'
[Unit]
Description=Synchronize B860H Wi-Fi client and AP channel
Wants=NetworkManager.service counter-inspect-mqtt-firewall.service
After=NetworkManager.service counter-inspect-mqtt-firewall.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/counter-inspect-b860h-ap-sync
TimeoutStartSec=50
EOF
cat >/etc/systemd/system/counter-inspect-b860h-ap.timer <<'EOF'
[Unit]
Description=Recover B860H hotspot and follow upstream Wi-Fi channel

[Timer]
OnBootSec=15
OnUnitInactiveSec=10
Unit=counter-inspect-b860h-ap.service

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now counter-inspect-b860h-ap.timer
# Apply new credentials on rerun even when the channel did not change.
if $CONFIGURE_AP && [[ "$active_uuid" == "$existing_uuid" && -n "$active_uuid" ]]; then
    nmcli --wait 15 connection down uuid "$active_uuid"
fi
if ! systemctl start counter-inspect-b860h-ap.service; then
    journalctl --no-pager -n 30 -u counter-inspect-b860h-ap.service >&2
    die "AP activation failed; the timer will retry. Check the service journal."
fi

cat <<EOF

B860H client + AP configuration installed.
Upstream: wlan0 (existing connection)
Hotspot:  wlan1 / $PROFILE
Gateway:  10.43.0.1 (MQTT 1883, OTA HTTP uses the configured Compose port)
Channel:  follows wlan0 every 10 seconds; last saved channel while offline
Profile:  $PROFILE_FILE (root-only WPA2 credentials)

Connect a phone or ESP32 to the AP and verify DHCP, MQTT and OTA access.
Check status: nmcli device status
Check logs:   journalctl -u counter-inspect-b860h-ap.service -n 50 --no-pager
Change SSID/password: sudo bash $0 --wifi-only --configure-ap
EOF
