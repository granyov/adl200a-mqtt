"""Unit tests for calibration: the 1910 certificate, the thermistor and installer scales.

    python3 -m unittest discover tests
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import calibration as cal

EXAMPLE = os.path.join(HERE, "..", "sensors.example.json")


class Cell1910(unittest.TestCase):
    """Calibration Sheet, model 1910 s/n 587968: Hz -> calculated pressure, kgf/cm2."""

    SHEET = [(2991.4, 0.001), (2863.1, 0.698), (2728.3, 1.400),
             (2587.0, 2.101), (2438.1, 2.801), (2280.4, 3.499)]

    def setUp(self):
        self.cell = cal.load_sensors(EXAMPLE)[1]

    def test_polynomial_reproduces_the_sheet(self):
        for hz, want in self.SHEET:
            self.assertAlmostEqual(self.cell.pressure(hz), want, delta=0.0005)

    def test_live_reading(self):
        out = self.cell.convert_values({"t01": 294720, "t06": 3344})
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["frequency_hz"], 2947.2)
        self.assertAlmostEqual(out["pressure"], 0.244, places=3)
        self.assertAlmostEqual(out["temperature_c"], 22.53, places=2)

    def test_thermistor_3k(self):
        self.assertAlmostEqual(cal.resistance_to_celsius(3000), 25.0, delta=0.02)
        self.assertIsNone(cal.resistance_to_celsius(239500))          # open input


class Rtd(unittest.TestCase):
    def test_pt100_table_points(self):                              # IEC 60751
        for ohms, t in ((100.0, 0.0), (138.5055, 100.0), (80.3063, -50.0), (175.856, 200.0)):
            self.assertAlmostEqual(cal.rtd_to_celsius(ohms, 100.0), t, delta=0.01)

    def test_pt1000_and_range(self):
        self.assertAlmostEqual(cal.rtd_to_celsius(1385.055, 1000.0), 100.0, delta=0.01)
        self.assertIsNone(cal.rtd_to_celsius(5.0, 100.0))


class InstallerScale(unittest.TestCase):
    def test_linear(self):
        s = cal.AnalogScale({"raw_multiplier": 0.001, "input_unit": "mV",
                             "linear": {"gage_factor": 2.0, "zero_reading": 0.5}, "unit": "kPa"})
        out = s.convert(12345)
        self.assertEqual((out["input"], out["value"], out["unit"]), (12.345, 23.69, "kPa"))

    def test_polynomial(self):
        s = cal.AnalogScale({"polynomial": [1.0, 2.0, 0.5]})
        self.assertEqual(s.convert(2)["value"], 1.0 + 4.0 + 2.0)

    def test_rtd_function(self):
        out = cal.AnalogScale({"raw_multiplier": 0.01, "function": "pt100"}).convert(10000)
        self.assertAlmostEqual(out["value"], 0.0, places=3)
        self.assertEqual(out["unit"], "°C")

    def test_without_conversion_only_the_input(self):
        out = cal.AnalogScale({"raw_multiplier": 0.1, "input_unit": "Ohm"}).convert(1234)
        self.assertEqual((out["status"], out["input"]), ("input_only", 123.4))
        self.assertNotIn("value", out)

    def test_problems(self):
        self.assertEqual(cal.AnalogScale.problems({"raw_multiplier": 0.01, "function": "pt100"}), [])
        self.assertEqual(len(cal.AnalogScale.problems(
            {"raw_multiplier": 0, "function": "pt50", "polynomial": "x"})), 4)


class AnalogChannel(unittest.TestCase):
    def write(self, channels):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as fh:
            json.dump({"channels": channels}, fh)
        self.addCleanup(os.remove, path)
        return path

    def test_mv_channel_without_vw(self):
        path = self.write({"05": {"name": "mV", "sweep": None, "analog": "mv",
                                  "analog_scale": {"raw_multiplier": 0.001,
                                                   "polynomial": [0.0, 10.0]}}})
        out = cal.load_sensors(path)[5].convert_values({"t02": 1500})
        self.assertEqual(out["status"], "ok")                  # no VW: no "нет струны"
        self.assertEqual(out["analog"]["value"], 15.0)
        self.assertEqual(out["analog"]["key"], "t02")

    def test_check_file(self):
        path = self.write({
            "05": {"name": "без масштаба", "sweep": None, "analog": "resistance"},
            "06": {"name": "плохой масштаб", "sweep": None, "analog": "bridge",
                   "analog_scale": {"linear": {"zero_reading": 1}}},
            "07": {"name": "лишний масштаб", "sweep": None, "analog": "thermistor_3k",
                   "analog_scale": {"raw_multiplier": 1}}})
        problems, notes = cal.check_file(path)
        self.assertEqual(len(problems), 1)
        self.assertIn("канал 06", problems[0])
        self.assertTrue(any("канал 05" in n and "сырое число" in n for n in notes))
        self.assertTrue(any("канал 07" in n and "своя кривая" in n for n in notes))

    def test_example_is_clean(self):
        self.assertEqual(cal.check_file(EXAMPLE), ([], []))


if __name__ == "__main__":
    unittest.main()
