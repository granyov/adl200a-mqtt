import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import adl200a_mqtt as bridge
import delivery


def frame(channel=1, typ=1, address=1, stamp="10:00:00"):
    return ("<<!01,%02d,MES:%02d,%02d,26/10/07,%s,0000012345;\r"
            % (address, channel, typ, stamp)).encode()


class SerialResponse:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.clock = 0
        self.sent = False

    def reset_input_buffer(self):
        pass

    def write(self, data):
        self.sent = True

    def flush(self):
        pass

    def read(self, size):
        self.clock += 0.5
        return self.chunks.pop(0) if self.sent and self.chunks else b""


class ProtocolTests(unittest.TestCase):
    def run_measure(self, chunks, expected):
        port = SerialResponse(chunks)
        with patch.object(bridge.time, "monotonic", side_effect=lambda: port.clock), \
                patch.object(bridge, "MEA_MAX_SECONDS", 6), \
                patch.object(bridge, "MEA_IDLE_DONE", 1):
            return bridge.measure(port, expected)

    def test_known_channels_survive_long_intermediate_silence(self):
        raw, reason = self.run_measure(
            [frame(1, 1) + frame(1, 6), b"", b"", b"", b"",
             frame(2, 1) + frame(2, 6)], {1, 2})
        self.assertEqual(reason, "complete")
        self.assertEqual(set(bridge.parse(raw)), {1, 2})

    def test_missing_pair_is_a_timeout_not_success(self):
        raw, reason = self.run_measure([frame(1, 1)], {1})
        self.assertEqual(reason, "timeout")
        self.assertNotIn("t06", bridge.parse(raw)[1]["values"])

    def test_frames_from_other_device_or_invalid_type_are_ignored(self):
        raw = frame(address=2) + frame(channel=17) + frame(typ=7) + frame()
        self.assertEqual(bridge.parse(raw)[1]["values"], {"t01": 12345})
        self.assertEqual(len(bridge.parse(raw)), 1)

    def test_pair_from_different_measurements_is_not_combined(self):
        parsed = bridge.parse(frame(typ=1) + frame(typ=6, stamp="10:00:01"))
        self.assertEqual(parsed[1]["values"], {"t06": 12345})

    def test_channels_from_different_sweeps_are_not_complete(self):
        _, reason = self.run_measure([
            frame(1, 1) + frame(1, 6)
            + frame(2, 1, stamp="10:00:01") + frame(2, 6, stamp="10:00:01")], {1, 2})
        self.assertEqual(reason, "timeout")

    def test_unknown_configuration_is_not_claimed_complete(self):
        _, reason = self.run_measure([frame() + frame(typ=6)], None)
        self.assertEqual(reason, "idle_unknown_channels")

    def read_gcc(self, response):
        port = SerialResponse([response])
        with patch.object(bridge.time, "monotonic", side_effect=lambda: port.clock):
            return bridge.enabled_channels(port)

    def test_duplicate_gcc_lines_do_not_count_as_16_channels(self):
        self.assertIsNone(self.read_gcc(b"<<!01,01,GCC:01,1,1,1,0020,6;\r" * 16))

    def test_confirmed_zero_enabled_channels_differs_from_unknown(self):
        response = b"".join(("<<!01,01,GCC:%02d,0,0,0,0000,0;\r" % ch).encode()
                            for ch in range(1, 17))
        self.assertEqual(self.read_gcc(response), set())


class SimulatedRestart(Exception):
    pass


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "pending.json")
        self.outbox = delivery.Outbox(self.path)
        self.messages = [["test/ch/01", json.dumps({"cycle_id": "fixed-cycle", "value": 7})],
                         ["test/measurement", json.dumps({"cycle_id": "fixed-cycle"})]]

    def client(self, info_factory):
        case = self

        class Client:
            def __init__(self):
                self.sent = []

            def is_connected(self):
                return True

            def publish(self, topic, payload, qos):
                case.assertTrue(os.path.exists(case.path))
                case.assertEqual(qos, 1)
                self.sent.append([topic, payload])
                return info_factory()

        return Client()

    def test_file_removed_only_after_all_acknowledgements(self):
        case = self

        class Ack:
            def wait_for_publish(self, timeout):
                case.assertTrue(os.path.exists(case.path))

            def is_published(self):
                return True

        self.outbox.stage("fixed-cycle", self.messages)
        client = self.client(Ack)
        self.outbox.flush(client)
        self.assertEqual(client.sent, self.messages)
        self.assertFalse(os.path.exists(self.path))

    def test_unacknowledged_file_survives_restart_with_same_payload_and_id(self):
        class NoAck:
            def wait_for_publish(self, timeout):
                raise SimulatedRestart()

        self.outbox.stage("fixed-cycle", self.messages)
        with self.assertRaises(SimulatedRestart):
            self.outbox.flush(self.client(NoAck))
        with open(self.path) as stream:
            self.assertEqual(json.load(stream)["cycle_id"], "fixed-cycle")
        self.recover()

    def recover(self):
        class Ack:
            def wait_for_publish(self, timeout):
                pass

            def is_published(self):
                return True

        if not os.path.exists(self.path):
            self.outbox.stage("fixed-cycle", self.messages)
        recovered = delivery.Outbox(self.path)
        client = self.client(Ack)
        recovered.flush(client)
        self.assertEqual(client.sent, self.messages)
        self.assertFalse(os.path.exists(self.path))

    def test_ack_timeout_keeps_same_message_instead_of_requeueing(self):
        class DelayedAck:
            def __init__(self):
                self.waits = 0

            def wait_for_publish(self, timeout):
                self.waits += 1

            def is_published(self):
                return self.waits >= 2

        self.outbox.stage("fixed-cycle", self.messages)
        client = self.client(DelayedAck)
        self.outbox.flush(client)
        self.assertEqual(len(client.sent), 2)

    def test_pending_response_cannot_be_overwritten(self):
        self.outbox.stage("fixed-cycle", self.messages)
        with self.assertRaises(RuntimeError):
            self.outbox.stage("other-cycle", [["test", "other"]])
        with open(self.path) as stream:
            self.assertEqual(json.load(stream)["cycle_id"], "fixed-cycle")


if __name__ == "__main__":
    unittest.main()
