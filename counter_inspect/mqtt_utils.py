from __future__ import annotations

import hashlib
import hmac

import paho.mqtt.client as mqtt

from .config import BrokerSettings, ConfigStore, RuntimeSettings


def scoped_client_id(client_id: str, key: bytes) -> str:
    """Keep broker client IDs unique per installation and stable across restarts."""
    suffix = hmac.new(key, b"counter-inspect-mqtt-instance-v1", hashlib.sha256).hexdigest()[:12]
    return f"{client_id}_{suffix}"


def create_client(
    client_id: str,
    broker: BrokerSettings,
    runtime: RuntimeSettings,
) -> mqtt.Client:
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=scoped_client_id(client_id, ConfigStore().read_key()),
        protocol=mqtt.MQTTv311,
    )
    if broker.credentials.username or broker.credentials.password:
        client.username_pw_set(broker.credentials.username, broker.credentials.password)
    client.reconnect_delay_set(
        min_delay=runtime.reconnect_min_seconds,
        max_delay=runtime.reconnect_max_seconds,
    )
    return client


def start_client(client: mqtt.Client, broker: BrokerSettings) -> None:
    client.connect_async(broker.host, broker.port, keepalive=60)
    client.loop_start()


def stop_client(client: mqtt.Client | None) -> None:
    if client is None:
        return
    try:
        client.disconnect()
    except Exception:
        pass
    try:
        client.loop_stop()
    except Exception:
        pass
