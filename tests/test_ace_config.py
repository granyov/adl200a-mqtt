"""Unit tests for ace_config: the SCC fields must match what ADL2Pro.exe builds.

    python3 -m unittest discover tests
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import ace_config as ace

EXAMPLE = os.path.join(HERE, "..", "sensors.example.json")


class VendorFields(unittest.TestCase):
    def test_factory_default_is_sweep01_thermistor(self):
        # what a fresh unit reports: <<!01,01,GCC:02,1,1,1,0020,6;
        self.assertEqual(ace.build_fields(1, 6), (1, 1, 1, 20, 6))
        self.assertEqual(ace.describe((1, 1, 1, 20, 6)),
                         "VW Sweep 01 (450–1200 Гц) + Thermistor 3K")

    def test_pressure_cell_1910(self):
        self.assertEqual(ace.build_fields(3, ace.ANALOG_TYPES["thermistor_3k"]),
                         (1, 1, 3, 20, 6))

    def test_inputs_off_and_analog_only(self):
        self.assertEqual(ace.build_fields(0, 0), ace.OFF)
        self.assertEqual(ace.build_fields(None, 6), (1, 0, 0, 0, 6))

    def test_serial_count_goes_to_f5(self):
        self.assertEqual(ace.build_fields(0, 4, serial_count=3), (1, 0, 0, 3, 4))
        with self.assertRaises(ace.ConfigError):
            ace.build_fields(0, 4)

    def test_bridge_and_serial_exclude_vw(self):
        for code in (3, 4):
            with self.assertRaises(ace.ConfigError):
                ace.build_fields(2, code, serial_count=1)

    def test_frame_types_follow_inputs(self):
        self.assertEqual(ace.frame_types((1, 1, 3, 20, 6)), [1, 6])
        self.assertEqual(ace.frame_types((1, 0, 0, 0, 2)), [2])
        self.assertEqual(ace.frame_types(ace.OFF), [])


class Sweep(unittest.TestCase):
    def test_cell_1910_needs_sweep_03(self):
        self.assertEqual(ace.pick_sweep(2280.4, 2991.4), 3)
        self.assertFalse(ace.covers(4, 2280.4, 2991.4))     # below 2400 Hz
        self.assertFalse(ace.covers(1, 2280.4, 2991.4))

    def test_narrowest_band_wins(self):
        self.assertEqual(ace.pick_sweep(900, 1100), 1)       # 1 and 2 both fit
        self.assertEqual(ace.pick_sweep(2500, 5000), 4)

    def test_no_band_fits(self):
        with self.assertRaises(ace.ConfigError):
            ace.pick_sweep(300, 500)


class Parsing(unittest.TestCase):
    def test_gcc_lines(self):
        text = ("<<!01,01,GCC:01,0,0,0,0000,0;\r<<!01,01,GCC:02,1,1,1,0020,6;\r"
                "<<!01,01,MES:02,06,26/09/30,16:09:42,0000239116;\r")
        self.assertEqual(ace.parse_gcc(text.encode()), {1: ace.OFF, 2: (1, 1, 1, 20, 6)})

    def test_recording(self):
        rec = ace.parse_recording(["<<!01,01,GAT:00,01;", "<<!01,01,GAM:01;", "<<!01,01,GOV:01;"])
        self.assertEqual(rec, {"GAT": (0, 1), "GAM": 1, "GOV": 1})
        self.assertEqual(ace.recording_commands({"GAT": (0, 15), "GOV": 0}),
                         [("GAT", "SAT:0,15"), ("GOV", "SOV:0")])

    def test_decode_names_every_value(self):
        item = ace.decode({1: (1, 1, 3, 20, 6), 2: ace.OFF})[0]
        self.assertEqual(item["channel"], 1)
        self.assertEqual(item["vw"], {"sweep": 3, "band_hz": [1400, 3500]})
        self.assertEqual(sorted(item["values"]), ["t01", "t06"])
        self.assertEqual(item["values"]["t06"]["unit"], "Ohm")


class Plan(unittest.TestCase):
    def write(self, doc):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as fh:
            json.dump(doc, fh)
        self.addCleanup(os.remove, path)
        return path

    def test_example_file(self):
        plans, rec, problems = ace.load_plan(EXAMPLE)
        self.assertEqual(problems, [])
        self.assertIsNone(rec)
        self.assertEqual(plans[1].fields, (1, 1, 3, 20, 6))

    def test_undeclared_inputs_are_an_error_not_off(self):
        path = self.write({"channels": {"01": {"name": "x"}}})
        plans, _rec, problems = ace.load_plan(path)
        self.assertEqual(plans, {})
        self.assertIn("не указано sweep и analog", problems[0])
        plans, _rec, problems = ace.load_plan(path, strict=False)
        self.assertEqual((plans, problems), ({}, []))

    def test_explicit_band_that_misses_the_sensor(self):
        path = self.write({"channels": {"01": {"sweep": 4, "analog": "thermistor_3k",
                                               "hz_min": 2280.4, "hz_max": 2991.4}}})
        _plans, _rec, problems = ace.load_plan(path)
        self.assertIn("подходит Sweep 03", problems[0])

    def test_recording_block(self):
        path = self.write({"channels": {},
                           "logger": {"recording": {"interval_min": 90, "mode": "periodic",
                                                    "when_full": "stop"}}})
        _plans, rec, problems = ace.load_plan(path)
        self.assertEqual(problems, [])
        self.assertEqual(rec, {"GAT": (1, 30), "GAM": 2, "GOV": 0})


if __name__ == "__main__":
    unittest.main()
