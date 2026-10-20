"""Critical memory accounting regressions; standard-library unittest only."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN / "helper"))
import ram_keeper as rk  # noqa: E402

MB = 1024 * 1024
GB = 1024 * MB


class CriticalMemoryTests(unittest.TestCase):
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
        def fake_reclaim(candidate, amount):
            calls.append(amount)
            return rk.Action(candidate.label, amount, amount, 0, "ok", amount)
        steward.reclaim_from = fake_reclaim
        steward.log = lambda _event: None
        snapshot = {
            "state": "critical", "free_percent": 1.0, "mem_total": 8 * GB,
            "mem_available": 80 * MB, "forecast": {},
            "groups": [
                {k: v for k, v in rk.asdict(item).items() if k != "path"}
                | {"path": str(item.path)} for item in (group, peer)
            ],
        }
        steward.relieve(snapshot=snapshot)
        self.assertEqual(calls, [75 * MB, 75 * MB])


if __name__ == "__main__":
    unittest.main(verbosity=2)
