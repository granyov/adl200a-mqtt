"""Exercise a real broker outage on a temporary port; production is untouched."""
import json
import os
import socket
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import delivery
import paho.mqtt.client as mqtt


def wait_for(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("integration test timed out")
        time.sleep(0.1)


def main():
    delivery.RETRY_SECONDS = 0.2
    delivery.ACK_TIMEOUT = 1
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="adl200a-broker-test-") as directory:
        log_file = open(os.path.join(directory, "broker.log"), "w")
        broker = None
        publisher = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                client_id="delivery-test-pub-" + uuid.uuid4().hex)
        receiver = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                               client_id="delivery-test-sub-" + uuid.uuid4().hex)
        ready = threading.Event()
        received = []
        errors = []
        cycle = uuid.uuid4().hex
        prefix = "test/adl200a/" + cycle
        receiver.on_connect = lambda c, u, f, r, p: c.subscribe(prefix + "/#", qos=1)
        receiver.on_subscribe = lambda *args: ready.set()
        receiver.on_disconnect = lambda *args: ready.clear()
        receiver.on_message = lambda c, u, m: received.append(json.loads(m.payload))
        for client in (publisher, receiver):
            client.reconnect_delay_set(min_delay=1, max_delay=2)

        class ReadyClient:
            def is_connected(self):
                return publisher.is_connected() and ready.is_set()

            def publish(self, *args, **kwargs):
                return publisher.publish(*args, **kwargs)

        def start_broker():
            binary = shutil.which("mosquitto") or "/usr/sbin/mosquitto"
            process = subprocess.Popen([binary, "-p", str(port)],
                                       stdout=log_file, stderr=log_file)
            time.sleep(0.3)
            if process.poll() is not None:
                raise AssertionError("test broker failed to start")
            return process

        try:
            broker = start_broker()
            for client in (publisher, receiver):
                client.connect_async("127.0.0.1", port, 5)
                client.loop_start()
            wait_for(lambda: publisher.is_connected() and ready.is_set())
            path = os.path.join(directory, "pending.json")
            staged = delivery.Outbox(path)
            messages = [[prefix + "/ch/04", json.dumps({"cycle_id": cycle, "value": 82189})],
                        [prefix + "/measurement", json.dumps({"cycle_id": cycle, "complete": True})]]
            staged.stage(cycle, messages)
            broker.terminate()
            broker.wait(timeout=5)
            broker = None
            wait_for(lambda: not publisher.is_connected() and not ready.is_set())
            # Recreate the outbox as a new process would after a restart.
            restored = delivery.Outbox(path)

            def deliver():
                try:
                    restored.flush(ReadyClient())
                except BaseException as error:
                    errors.append(error)

            worker = threading.Thread(target=deliver, daemon=True)
            worker.start()
            time.sleep(1)
            assert worker.is_alive() and os.path.exists(path), "response disappeared while offline"
            broker = start_broker()
            worker.join(timeout=15)
            assert not worker.is_alive(), "delivery did not recover"
            assert not errors, errors
            wait_for(lambda: len(received) == 2)
            assert all(message["cycle_id"] == cycle for message in received)
            assert not os.path.exists(path), "acknowledged response was not removed"
            print("PASS: broker outage, saved response recovery, QoS 1 PUBACKs, subscriber reception")
        finally:
            for client in (publisher, receiver):
                client.disconnect()
                client.loop_stop()
            if broker is not None:
                broker.terminate()
                broker.wait(timeout=5)
            log_file.close()


if __name__ == "__main__":
    main()
