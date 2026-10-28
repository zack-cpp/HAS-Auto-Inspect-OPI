from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path

import paho.mqtt.client as mqtt

from .config import ConfigStore, Settings, SettingsWatcher
from .logging_utils import configure_logging
from .mqtt_utils import create_client, start_client, stop_client
from .runtime import Shutdown, service_loop


LOCAL_TOPICS = (
    "counter/mesin/jobsend",
    "counter/mesin",
    "counter/running",
    "config/return",
    "counter/info",
    "counter/label",
    "counter/label-sku",
)
REMOTE_TOPICS = ("return/inspection/startjob", "return/error", "config/config")


class BridgeService:
    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger
        self.local_client: mqtt.Client | None = None
        self.remote_client: mqtt.Client | None = None
        self._client_lock = threading.RLock()
        self._queue_lock = threading.Lock()

    def start(self) -> None:
        self._replace_local()
        self._replace_remote()
        self.logger.info("MQTT bridge started for device %s", self.settings.device_id)

    def stop(self) -> None:
        with self._client_lock:
            local, remote = self.local_client, self.remote_client
            self.local_client = None
            self.remote_client = None
        stop_client(local)
        stop_client(remote)
        self.logger.info("MQTT bridge stopped")

    def reload(self, candidate: Settings) -> None:
        previous = self.settings
        device_changed = candidate.device_id != previous.device_id
        reconnect_changed = (
            candidate.runtime.reconnect_min_seconds != previous.runtime.reconnect_min_seconds
            or candidate.runtime.reconnect_max_seconds != previous.runtime.reconnect_max_seconds
        )
        local_changed = candidate.local_broker != previous.local_broker or device_changed or reconnect_changed
        remote_changed = candidate.remote_broker != previous.remote_broker or device_changed or reconnect_changed
        self.settings = candidate
        if candidate.storage.log_dir != previous.storage.log_dir or candidate.runtime.log_level != previous.runtime.log_level:
            self.logger = configure_logging("mqtt-bridge", candidate.storage.log_dir, candidate.runtime.log_level)
        if local_changed:
            self._replace_local()
        if remote_changed:
            self._replace_remote()
        if candidate.storage.queue_dir != previous.storage.queue_dir:
            candidate.storage.queue_dir.mkdir(parents=True, exist_ok=True)
        self.logger.info("Applied updated configuration")

    def _replace_local(self) -> None:
        client = create_client(
            f"counter_bridge_local_{self.settings.device_id}",
            self.settings.local_broker,
            self.settings.runtime,
        )
        client.on_connect = self._on_connect_local
        client.on_message = self._on_message_local
        client.on_disconnect = self._on_disconnect_local
        with self._client_lock:
            previous = self.local_client
            self.local_client = client
        stop_client(previous)
        start_client(client, self.settings.local_broker)

    def _replace_remote(self) -> None:
        client = create_client(
            f"counter_bridge_remote_{self.settings.device_id}",
            self.settings.remote_broker,
            self.settings.runtime,
        )
        client.on_connect = self._on_connect_remote
        client.on_message = self._on_message_remote
        client.on_disconnect = self._on_disconnect_remote
        with self._client_lock:
            previous = self.remote_client
            self.remote_client = client
        stop_client(previous)
        start_client(client, self.settings.remote_broker)

    def _on_connect_local(self, client, _userdata, _flags, reason_code, _properties) -> None:
        if reason_code == 0:
            self.logger.info("Connected to local MQTT broker")
            for topic in LOCAL_TOPICS:
                client.subscribe(topic)
        else:
            self.logger.error("Local MQTT connection failed: %s", reason_code)

    def _on_connect_remote(self, client, _userdata, _flags, reason_code, _properties) -> None:
        if reason_code == 0:
            self.logger.info("Connected to remote MQTT broker")
            for topic in REMOTE_TOPICS:
                client.subscribe(topic)
            self.flush_queue()
        else:
            self.logger.error("Remote MQTT connection failed: %s", reason_code)

    def _on_disconnect_local(self, _client, _userdata, _flags, reason_code, _properties) -> None:
        if reason_code != 0:
            self.logger.warning("Disconnected from local MQTT broker: %s", reason_code)

    def _on_disconnect_remote(self, _client, _userdata, _flags, reason_code, _properties) -> None:
        if reason_code != 0:
            self.logger.warning("Disconnected from remote MQTT broker: %s", reason_code)

    def _on_message_local(self, _client, _userdata, message) -> None:
        try:
            payload = message.payload.decode("utf-8")
        except UnicodeDecodeError:
            self.logger.warning("Ignored non-UTF-8 payload on %s", message.topic)
            return
        self.logger.info("Local MQTT received | %s: %s", message.topic, payload)
        with self._client_lock:
            remote = self.remote_client
        try:
            if remote is None:
                raise RuntimeError("remote client is unavailable")
            result = remote.publish(message.topic, payload)
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                # QoS 0 success means accepted by the client library, not an
                # acknowledgement from the remote broker.
                self.logger.info("Queued for remote MQTT | %s", message.topic)
            else:
                self.logger.warning("Remote MQTT publish failed on %s: code %s", message.topic, result.rc)
                if message.topic == LOCAL_TOPICS[0]:
                    self.queue_save(payload)
        except Exception as exc:
            self.logger.warning("Remote publish failed: %s", exc)
            if message.topic == LOCAL_TOPICS[0]:
                self.queue_save(payload)

    def _on_message_remote(self, _client, _userdata, message) -> None:
        try:
            payload = message.payload.decode("utf-8")
            document = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.logger.warning("Ignored invalid remote payload on %s", message.topic)
            return
        if document.get("mesin_id") != self.settings.device_id:
            return
        with self._client_lock:
            local = self.local_client
        try:
            if local is None:
                raise RuntimeError("local client is unavailable")
            result = local.publish(message.topic, payload)
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                self.logger.info("Remote -> local | %s: %s", message.topic, payload)
            else:
                self.logger.warning("Local publish rejected with code %s", result.rc)
        except Exception as exc:
            self.logger.warning("Local publish failed: %s", exc)

    def _queue_files(self) -> list[Path]:
        queue_dir = self.settings.storage.queue_dir
        queue_dir.mkdir(parents=True, exist_ok=True)
        return sorted(queue_dir.glob("jobsend_*.txt"))

    def queue_save(self, payload: str) -> None:
        clean_payload = payload.replace("\r", "").replace("\n", "")
        queue_dir = self.settings.storage.queue_dir
        queue_dir.mkdir(parents=True, exist_ok=True)
        path = queue_dir / f"jobsend_{datetime.now():%Y-%m-%d}.txt"
        with self._queue_lock, path.open("a", encoding="utf-8") as handle:
            handle.write(clean_payload + "\n")
        self.logger.warning("Saved jobsend message to persistent queue")

    def flush_queue(self) -> None:
        with self._client_lock:
            remote = self.remote_client
        if remote is None:
            return
        with self._queue_lock:
            for path in self._queue_files():
                try:
                    messages = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
                except OSError as exc:
                    self.logger.error("Cannot read queue file %s: %s", path, exc)
                    continue
                remaining: list[str] = []
                for payload in messages:
                    try:
                        result = remote.publish(LOCAL_TOPICS[0], payload)
                        if result.rc != mqtt.MQTT_ERR_SUCCESS:
                            remaining.append(payload)
                    except Exception:
                        remaining.append(payload)
                try:
                    if remaining:
                        temporary = path.with_suffix(".tmp")
                        temporary.write_text("\n".join(remaining) + "\n", encoding="utf-8")
                        temporary.replace(path)
                    else:
                        path.unlink(missing_ok=True)
                except OSError as exc:
                    self.logger.error("Cannot update queue file %s: %s", path, exc)
                if messages:
                    self.logger.info("Flushed %d of %d queued messages", len(messages) - len(remaining), len(messages))


def main() -> int:
    store = ConfigStore()
    settings = store.load()
    logger = configure_logging("mqtt-bridge", settings.storage.log_dir, settings.runtime.log_level)
    watcher = SettingsWatcher(store, lambda message: logger.error("Rejected configuration update: %s", message))
    service = BridgeService(settings, logger)
    shutdown = Shutdown()
    shutdown.install()
    service.start()
    try:
        service_loop(
            shutdown,
            lambda: watcher.settings.runtime.config_poll_seconds,
            lambda: service.reload(candidate) if (candidate := watcher.poll()) else None,
        )
    finally:
        service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
