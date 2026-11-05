"""Critical memory accounting regressions; standard-library unittest only."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("RAM_KEEPER_CONFIG", str(Path(tempfile.mkdtemp(prefix="rk-config-")) / "config.json"))
os.environ.setdefault("RAM_KEEPER_STATE_DIR", tempfile.mkdtemp(prefix="rk-state-"))

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN / "helper"))
import ram_keeper as rk  # noqa: E402

MB = 1024 * 1024
GB = 1024 * MB


class CriticalMemoryTests(unittest.TestCase):
    def test_missing_memtotal_publishes_unknown_not_negative_metrics(self):
        steward = rk.Steward(config=dict(rk.DEFAULT_CONFIG))
        steward.last_forecast.will_cross = True
        steward.last_forecast.horizon_seconds = 90
        psi = {"some_avg10": 0, "some_avg60": 0, "some_avg300": 0,
               "full_avg10": 0, "full_avg60": 0, "full_avg300": 0,
               "available": True}
        with (
            patch("ram_keeper.meminfo", return_value={"MemAvailable": 100, "Cached": 200}),
            patch("ram_keeper.psi", return_value=psi),
            patch("ram_keeper.zram_stats", return_value={"devices": [], "used": 0, "size": 0}),
            patch("ram_keeper.user_root", return_value=Path("/nonexistent")),
        ):
            result = steward.snapshot()
        self.assertEqual(result["state"], "unknown")
        self.assertFalse(result["memory_reading_valid"])
        for key in ("mem_total", "mem_available", "free_percent", "mem_used", "cached", "anon_and_kernel"):
            self.assertIsNone(result[key], key)
        self.assertIsNone(result["forecast"])
        self.assertFalse(steward.last_forecast.will_cross)

    def test_missing_cached_marks_breakdown_unavailable_but_keeps_free_reading(self):
        steward = rk.Steward(config=dict(rk.DEFAULT_CONFIG))
        psi = {"some_avg10": 0, "some_avg60": 0, "some_avg300": 0,
               "full_avg10": 0, "full_avg60": 0, "full_avg300": 0,
               "available": True}
        with (
            patch("ram_keeper.meminfo", return_value={"MemTotal": 1000, "MemAvailable": 300}),
            patch("ram_keeper.psi", return_value=psi),
            patch("ram_keeper.zram_stats", return_value={"devices": [], "used": 0, "size": 0}),
            patch("ram_keeper.user_root", return_value=Path("/nonexistent")),
        ):
            result = steward.snapshot()
        self.assertTrue(result["memory_reading_valid"])
        self.assertFalse(result["memory_breakdown_valid"])
        self.assertEqual(result["free_percent"], 30.0)
        self.assertEqual(result["mem_used"], 700)
        self.assertIsNone(result["cached"])
        self.assertIsNone(result["anon_and_kernel"])

    def test_forced_reclaim_is_blocked_when_memory_reading_is_unknown(self):
        steward = rk.Steward(config=dict(rk.DEFAULT_CONFIG))
        candidate = {
            "name": "candidate.scope", "label": "candidate", "kind": "scope",
            "path": "/nonexistent", "current": 300 * MB,
            "anon": 100 * MB, "file": 200 * MB,
            "reclaimable_file": 200 * MB, "reclaimable": 200 * MB,
            "swap": 0, "swapable": True, "pids": 1, "protected": False,
        }
        calls = []
        def fake_reclaim(group, amount, cache_only=False):
            calls.append(amount)
            return rk.Action(group.label, amount, 0, 0, "ok", 0)
        steward.reclaim_from = fake_reclaim
        snapshot = {
            "state": "unknown", "free_percent": 1.0, "mem_total": 8 * GB,
            "mem_available": 80 * MB, "forecast": {}, "groups": [candidate],
        }
        result = steward.relieve(snapshot=snapshot, force=True)
        self.assertFalse(result["acted"])
        self.assertEqual(result["actions"], [])
        self.assertEqual(calls, [])

    def test_missing_psi_file_is_unavailable_not_zero_pressure(self):
        with patch("ram_keeper.Path.read_text", side_effect=FileNotFoundError):
            result = rk.psi()
        self.assertFalse(result["available"])
        self.assertEqual(result["some_avg60"], 0.0)

    def test_valid_zero_psi_is_available(self):
        psi_text = (
            "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
            "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
        )
        with patch("ram_keeper.Path.read_text", return_value=psi_text):
            result = rk.psi()
        self.assertTrue(result["available"])
        self.assertEqual(result["some_avg60"], 0.0)

    def test_zero_memavailable_is_critical_not_unknown(self):
        steward = rk.Steward(config=dict(rk.DEFAULT_CONFIG))
        steward.publish = lambda _snapshot: None
        psi = {"some_avg10": 0, "some_avg60": 0, "some_avg300": 0,
               "full_avg10": 0, "full_avg60": 0, "full_avg300": 0}
        with patch("ram_keeper.meminfo", return_value={"MemTotal": 8 * GB, "MemAvailable": 0, "Cached": 0}), \
             patch("ram_keeper.psi", return_value=psi), \
             patch("ram_keeper.zram_stats", return_value={"devices": [], "used": 0, "size": 0}), \
             patch("ram_keeper.user_root", return_value=Path("/nonexistent")):
            result = steward.snapshot()
        self.assertEqual(result["state"], "critical")
        self.assertEqual(result["mem_available"], 0)

    def test_victim_slice_is_fraction_of_that_victims_memory(self):
        steward = rk.Steward(config=dict(rk.DEFAULT_CONFIG))
        self.assertEqual(steward.max_victim_slice(300 * MB), 75 * MB)
        self.assertEqual(steward.max_victim_slice(2 * GB), 512 * MB)

    def test_reclaim_request_never_exceeds_fraction_of_small_victim(self):
        steward = rk.Steward(config=dict(rk.DEFAULT_CONFIG))
        group = rk.Cgroup(path=Path("/nonexistent"), name="small.scope", kind="scope",
                          label="small", current=300 * MB, anon=300 * MB)
        peer = rk.Cgroup(path=Path("/nonexistent-peer"), name="peer.scope", kind="scope",
                         label="peer", current=300 * MB, anon=300 * MB)
        calls = []
        def fake_reclaim(candidate, amount, cache_only=False):
            calls.append(amount)
            return rk.Action(candidate.label, amount, amount, 0, "ok", amount)
        steward.reclaim_from = fake_reclaim
        steward.log = lambda _event: None
        snapshot = {
            "state": "critical", "free_percent": 1.0, "mem_total": 8 * GB,
            "mem_available": 80 * MB, "forecast": {},
            "zram": {"devices": [{"name": "/dev/zram0", "ram_backed": True,
                                  "size": 8 * GB, "used": 0}]},
            "groups": [
                {k: v for k, v in rk.asdict(item).items() if k != "path"}
                | {"path": str(item.path)} for item in (group, peer)
            ],
        }
        steward.relieve(snapshot=snapshot)
        self.assertEqual(calls, [75 * MB, 75 * MB])


if __name__ == "__main__":
    unittest.main(verbosity=2)
