"""End-to-end against tests/fake_logger.py: channels.py --apply and the daemon's reads.

Needs pyserial (and paho-mqtt for the daemon part); skipped otherwise.

    python3 -m unittest discover tests
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, ROOT)

try:
    import serial
except ImportError:
    serial = None
try:
    import paho.mqtt.client  # noqa: F401
except ImportError:
    paho = None
else:
    paho = True


@unittest.skipIf(serial is None, "pyserial is not installed")
class WithFakeLogger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        # the office unit as left on 2026-09-30: 01-14 on Sweep 01, the cell on 01
        self.fake = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "fake_logger.py"), "--preset", "office",
             "--sensor", "1:2947.2:3344", "--per-channel", "0.05"],
            stdout=subprocess.PIPE, universal_newlines=True)
        self.addCleanup(self.fake.wait)
        self.addCleanup(self.fake.terminate)
        self.port = self.fake.stdout.readline().strip()

    def channels(self, *args):
        cmd = [sys.executable, os.path.join(ROOT, "channels.py"), "--port", self.port,
               "--no-service", "--backup", os.path.join(self.tmp, "backup.txt")] + list(args)
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True, timeout=240)
        return p.returncode, p.stdout

    def test_apply_example_then_restore(self):
        sensors = os.path.join(ROOT, "sensors.example.json")
        code, out = self.channels("--sensors", sensors, "--apply")
        self.assertEqual(code, 0, out)
        self.assertIn("SCC:1,1,1,3,20,6", out)
        self.assertIn("сухой прогон", out)

        code, out = self.channels("--sensors", sensors, "--apply", "--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("всё применилось", out)

        code, out = self.channels("--sensors", sensors, "--apply")
        self.assertIn("всё уже как надо", out)

        code, out = self.channels("--restore", os.path.join(self.tmp, "backup.txt"), "--yes")
        self.assertIn("всё применилось", out)
        self.assertIn("включено каналов: 14", out)

    def test_recording_is_written_and_verified(self):
        doc = json.load(open(os.path.join(ROOT, "sensors.example.json")))
        doc["logger"] = {"recording": {"interval_min": 15, "mode": "fixed",
                                       "when_full": "overwrite"}}
        path = os.path.join(self.tmp, "sensors.json")
        json.dump(doc, open(path, "w"))
        code, out = self.channels("--sensors", path, "--apply", "--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("SAT:0,15", out)
        self.assertNotIn("SAM:", out)            # already Fixed
        self.assertIn("всё применилось", out)

    def test_bad_file_never_touches_the_logger(self):
        path = os.path.join(self.tmp, "bad.json")
        json.dump({"channels": {"01": {"sweep": 2, "analog": "bridge"}}}, open(path, "w"))
        code, out = self.channels("--sensors", path, "--apply", "--yes")
        self.assertNotEqual(code, 0)
        self.assertIn("прибор не трогаю", out)
        self.assertNotIn("сейчас в приборе", out)

    @unittest.skipIf(paho is None, "paho-mqtt is not installed")
    def test_daemon_waits_for_configured_frames(self):
        os.environ["ADL200A_PORT"] = self.port
        import adl200a_mqtt as d
        ser = serial.Serial(self.port, 38400, timeout=0.5)
        self.addCleanup(ser.close)
        cfg = d.read_channel_config(ser)
        self.assertEqual(len(cfg), 16)
        expected = d.expected_frames(cfg)
        self.assertEqual(sorted(expected), list(range(1, 15)))
        chans = d.parse(d.measure(ser, expected))
        self.assertEqual(sorted(chans), list(range(1, 15)))
        # Sweep 01 does not cover the cell's 2947 Hz: no VW reading, like on site
        self.assertEqual(chans[1]["values"], {"t01": 0, "t06": 3344})


if __name__ == "__main__":
    unittest.main()
