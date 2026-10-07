"""One pending MQTT batch, kept on disk until every QoS 1 PUBACK arrives.

There is no measurement archive. Polling pauses while this batch is pending.
After a process restart, a batch may be sent again with the same cycle_id.
"""
import json
import fcntl
import logging
import os
import tempfile
import time

log = logging.getLogger("adl200a")
RETRY_SECONDS = 5
ACK_TIMEOUT = 10


def wait_connected(client):
    warned = False
    while not client.is_connected():
        if not warned:
            log.warning("MQTT unavailable; waiting before the next serial command")
            warned = True
        time.sleep(RETRY_SECONDS)


class Outbox:
    def __init__(self, path):
        self.path = os.path.abspath(path)

    def lock(self):
        os.makedirs(os.path.dirname(self.path), mode=0o770, exist_ok=True)
        self._lock_file = open(self.path + ".lock", "a")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock_file.close()
            raise RuntimeError("another ADL-200A delivery process is already running")

    def _sync_directory(self):
        fd = os.open(os.path.dirname(self.path), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def stage(self, cycle_id, messages):
        """Atomically save the batch before handing any message to MQTT."""
        if os.path.exists(self.path):
            raise RuntimeError("a pending MQTT batch must be delivered first")
        directory = os.path.dirname(self.path)
        os.makedirs(directory, mode=0o770, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "cycle_id": cycle_id,
                           "messages": messages}, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            self._sync_directory()
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def flush(self, client):
        """Keep the file and wait/retry until the broker confirms every message."""
        if not os.path.exists(self.path):
            return
        with open(self.path, encoding="utf-8") as stream:
            batch = json.load(stream)
        if (batch.get("version") != 1 or not batch.get("cycle_id")
                or not isinstance(batch.get("messages"), list)
                or not batch["messages"]):
            raise ValueError("invalid pending MQTT batch; file kept for recovery")
        for message in batch["messages"]:
            if (not isinstance(message, list) or len(message) != 2
                    or not all(isinstance(value, str) for value in message)):
                raise ValueError("invalid pending MQTT message; file kept for recovery")
        log.info("Delivering cycle %s: %d MQTT messages, QoS 1",
                 batch["cycle_id"], len(batch["messages"]))
        for topic, payload in batch["messages"]:
            info = None
            while True:
                if info is None:
                    wait_connected(client)
                    info = client.publish(topic, payload, qos=1)
                try:
                    info.wait_for_publish(timeout=ACK_TIMEOUT)
                    if info.is_published():
                        break
                    # Keep the same Paho message: it resends unacknowledged
                    # QoS 1 packets on reconnect. A timeout is not a PUBACK.
                    log.warning("Still waiting for MQTT PUBACK: %s; cycle %s kept",
                                topic, batch["cycle_id"])
                except (RuntimeError, ValueError, OSError) as error:
                    # A connection can disappear between is_connected() and
                    # publish(). Re-enqueue after reconnect, with the same ID.
                    log.warning("MQTT publish failed: %s; retrying cycle %s",
                                error, batch["cycle_id"])
                    info = None
                    time.sleep(RETRY_SECONDS)
        # All messages were acknowledged. Failure to remove the file must not
        # allow a new measurement to overwrite it.
        while True:
            try:
                if os.path.exists(self.path):
                    os.unlink(self.path)
                self._sync_directory()
                break
            except OSError as error:
                log.error("Cannot clear acknowledged MQTT batch: %s", error)
                time.sleep(RETRY_SECONDS)
        log.info("MQTT broker confirmed cycle %s (%d messages)",
                 batch["cycle_id"], len(batch["messages"]))
