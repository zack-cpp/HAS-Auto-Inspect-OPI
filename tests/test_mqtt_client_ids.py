from __future__ import annotations

from counter_inspect.config import ConfigStore, clone_default_credentials, clone_default_runtime, parse_settings
from counter_inspect.mqtt_utils import create_client, scoped_client_id


def test_client_ids_are_stable_per_installation_and_distinct_across_keys():
    key_a = bytes(range(32))
    key_b = bytes(reversed(range(32)))
    bridge_id = scoped_client_id("counter_bridge_remote_HAS-AI-0059", key_a)

    assert bridge_id == scoped_client_id("counter_bridge_remote_HAS-AI-0059", key_a)
    assert bridge_id != scoped_client_id("counter_bridge_remote_HAS-AI-0059", key_b)
    assert bridge_id != scoped_client_id("counter_ota_HAS-AI-0059", key_a)
    assert bridge_id.startswith("counter_bridge_remote_HAS-AI-0059_")


def test_create_client_uses_the_installation_key(monkeypatch):
    key = bytes(range(32))
    monkeypatch.setattr(ConfigStore, "read_key", lambda _self: key)
    settings = parse_settings(clone_default_runtime(), clone_default_credentials())

    client = create_client("counter_bridge_remote_HAS-AI-0059", settings.remote_broker, settings.runtime)

    assert client._client_id.decode() == scoped_client_id("counter_bridge_remote_HAS-AI-0059", key)
