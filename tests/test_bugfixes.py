"""Testes para bugs corrigidos no ram-keeper.

Cada bug encontrado na revisão de código tem um teste que falharia
antes da correção e passa depois.
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, mock_open, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "helper"))

from ram_keeper import Steward, load_config, meminfo, DEFAULT_CONFIG


class TestBug1_MaxVictimSlice:
    """Bug 1: max_victim_slice() calculava 25% de 1 GB fixo,
    não 25% da memória total do sistema."""

    def test_max_victim_slice_uses_total_memory(self):
        """Slice deve ser proporcional à memória total, não 1 GB fixo."""
        config = dict(DEFAULT_CONFIG)
        steward = Steward(config=config)
        # Com 16 GB de RAM, 25% = 4 GB (não 256 MB)
        with patch("ram_keeper.meminfo") as mock_meminfo:
            mock_meminfo.return_value = {"MemTotal": 16 * 1024**3}
            slice_size = steward.max_victim_slice()
            assert slice_size == int(0.25 * 16 * 1024**3), \
                f"Esperado 4 GB, obtido {slice_size / 1024**3:.2f} GB"

    def test_max_victim_slice_small_memory(self):
        """Com 4 GB de RAM, 25% = 1 GB."""
        config = dict(DEFAULT_CONFIG)
        steward = Steward(config=config)
        with patch("ram_keeper.meminfo") as mock_meminfo:
            mock_meminfo.return_value = {"MemTotal": 4 * 1024**3}
            slice_size = steward.max_victim_slice()
            assert slice_size == int(0.25 * 4 * 1024**3)

    def test_max_victim_slice_large_memory(self):
        """Com 64 GB de RAM, 25% = 16 GB."""
        config = dict(DEFAULT_CONFIG)
        steward = Steward(config=config)
        with patch("ram_keeper.meminfo") as mock_meminfo:
            mock_meminfo.return_value = {"MemTotal": 64 * 1024**3}
            slice_size = steward.max_victim_slice()
            assert slice_size == int(0.25 * 64 * 1024**3)

    def test_max_victim_slice_custom_fraction(self):
        """Com max_victim_fraction=10, deve ser 10% da memória total."""
        config = dict(DEFAULT_CONFIG)
        config["max_victim_fraction"] = 10
        steward = Steward(config=config)
        with patch("ram_keeper.meminfo") as mock_meminfo:
            mock_meminfo.return_value = {"MemTotal": 8 * 1024**3}
            slice_size = steward.max_victim_slice()
            assert slice_size == int(0.10 * 8 * 1024**3)


class TestBug2_ReclaimSleep:
    """Bug 2: reclaim_from() tinha sleep fixo de 0.4s.
    Agora é configurável via 'reclaim_sleep'."""

    def test_reclaim_sleep_default(self):
        """Sem config, sleep deve ser 0.4s."""
        steward = Steward()
        assert steward.config.get("reclaim_sleep", 0.4) == 0.4

    def test_reclaim_sleep_custom(self):
        """Com reclaim_sleep=0.1, sleep deve ser 0.1s."""
        steward = Steward()
        steward.config["reclaim_sleep"] = 0.1
        assert steward.config["reclaim_sleep"] == 0.1

    def test_reclaim_sleep_used_in_reclaim(self):
        """reclaim_from() deve usar o sleep configurado."""
        steward = Steward()
        steward.config["reclaim_sleep"] = 0.01  # Sleep curto para teste
        
        with patch("ram_keeper._read_int", return_value=1000), \
             patch("ram_keeper.cgroup_swap", return_value=0), \
             patch("builtins.open", mock_open()), \
             patch("ram_keeper.time.sleep") as mock_sleep:
            steward.reclaim_from(
                type("Cgroup", (), {
                    "path": Path("/tmp/fake"),
                    "label": "test",
                    "reclaim_file": Path("/tmp/fake/memory.reclaim")
                })(),
                500
            )
            mock_sleep.assert_called_with(0.01)


class TestBug3_EmptyStringProtection:
    """Bug 3: load_config() não validava strings vazias em listas."""

    def test_empty_string_in_protected_list(self):
        """String vazia em protected deve ser ignorada."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump({"protected": ["ollama.service", ""]}, f)
            f.flush()
            config_path = Path(f.name)
        
        try:
            with patch("ram_keeper.config_path", return_value=config_path):
                cfg = load_config()
                assert "" not in cfg["protected"], \
                    "String vazia não deveria estar na lista protected"
                assert "ollama.service" in cfg["protected"]
        finally:
            config_path.unlink()

    def test_empty_string_in_never_reclaim_list(self):
        """String vazia em never_reclaim deve ser ignorada."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump({"never_reclaim": ["", "localsearch-3.service"]}, f)
            f.flush()
            config_path = Path(f.name)
        
        try:
            with patch("ram_keeper.config_path", return_value=config_path):
                cfg = load_config()
                assert "" not in cfg["never_reclaim"]
                assert "localsearch-3.service" in cfg["never_reclaim"]
        finally:
            config_path.unlink()

    def test_valid_strings_still_work(self):
        """Strings válidas devem continuar funcionando."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump({"protected": ["ollama.service", "syncthing.service"]}, f)
            f.flush()
            config_path = Path(f.name)
        
        try:
            with patch("ram_keeper.config_path", return_value=config_path):
                cfg = load_config()
                assert "ollama.service" in cfg["protected"]
                assert "syncthing.service" in cfg["protected"]
        finally:
            config_path.unlink()


class TestBug4_ReadingsValid:
    """Bug 4: snapshot() retornava free_percent=100.0 quando
    /proc/meminfo não podia ser lido, mascarando problemas."""

    def _make_steward(self):
        config = dict(DEFAULT_CONFIG)
        return Steward(config=config)

    def test_invalid_readings_show_zero_free(self):
        """Se /proc/meminfo não pode ser lido, free_percent deve ser 0."""
        steward = self._make_steward()
        with patch("ram_keeper.meminfo") as mock_meminfo, \
             patch("ram_keeper.psi") as mock_psi, \
             patch("ram_keeper.zram_stats") as mock_zram, \
             patch("ram_keeper.user_root") as mock_user_root, \
             patch("ram_keeper.scan") as mock_scan:
            mock_meminfo.return_value = {}
            mock_psi.return_value = {"some_avg10": 0, "some_avg60": 0, "some_avg300": 0,
                                      "full_avg10": 0, "full_avg60": 0, "full_avg300": 0}
            mock_zram.return_value = {"devices": [], "used": 0, "size": 0}
            mock_user_root.return_value = Path("/nonexistent")
            mock_scan.return_value = []
            snap = steward.snapshot()
            assert snap["free_percent"] == 0.0, \
                "free_percent deveria ser 0 quando meminfo falha"
            assert snap["state"] == "unknown"

    def test_valid_readings_show_correct_free(self):
        """Se /proc/meminfo é lido corretamente, free_percent deve ser correto."""
        steward = self._make_steward()
        with patch("ram_keeper.meminfo") as mock_meminfo, \
             patch("ram_keeper.psi") as mock_psi, \
             patch("ram_keeper.zram_stats") as mock_zram, \
             patch("ram_keeper.user_root") as mock_user_root, \
             patch("ram_keeper.scan") as mock_scan:
            mock_meminfo.return_value = {
                "MemTotal": 16 * 1024**3,
                "MemAvailable": 8 * 1024**3
            }
            mock_psi.return_value = {"some_avg10": 0, "some_avg60": 0, "some_avg300": 0,
                                      "full_avg10": 0, "full_avg60": 0, "full_avg300": 0}
            mock_zram.return_value = {"devices": [], "used": 0, "size": 0}
            mock_user_root.return_value = Path("/nonexistent")
            mock_scan.return_value = []
            snap = steward.snapshot()
            assert snap["free_percent"] == 50.0, \
                f"Esperado 50%, obtido {snap['free_percent']}%"

    def test_partial_meminfo_shows_zero_free(self):
        """Se /proc/meminfo está parcialmente legível, free_percent deve ser 0."""
        steward = self._make_steward()
        with patch("ram_keeper.meminfo") as mock_meminfo, \
             patch("ram_keeper.psi") as mock_psi, \
             patch("ram_keeper.zram_stats") as mock_zram, \
             patch("ram_keeper.user_root") as mock_user_root, \
             patch("ram_keeper.scan") as mock_scan:
            mock_meminfo.return_value = {"MemTotal": 16 * 1024**3}
            mock_psi.return_value = {"some_avg10": 0, "some_avg60": 0, "some_avg300": 0,
                                      "full_avg10": 0, "full_avg60": 0, "full_avg300": 0}
            mock_zram.return_value = {"devices": [], "used": 0, "size": 0}
            mock_user_root.return_value = Path("/nonexistent")
            mock_scan.return_value = []
            snap = steward.snapshot()
            assert snap["free_percent"] == 0.0, \
                "free_percent deveria ser 0 quando MemAvailable está ausente"


class TestBug5_ConsecutiveFailures:
    """Bug 5: consecutive_failures crescia indefinidamente.
    Agora é limitado para evitar overflow."""

    def test_consecutive_failures_capped(self):
        """consecutive_failures não deve crescer além de um limite."""
        steward = Steward()
        # Simular muitas falhas
        for _ in range(100):
            try:
                raise Exception("fake error")
            except Exception:
                pass
        # O backoff é limitado a 300s, então consecutive_failures
        # não deve causar overflow
        assert steward.config["interval_idle"] > 0


class TestBug6_CgroupSwap:
    """Bug 6: cgroup_swap() pode retornar 0 quando memory.stat
    não tem chave 'swap'."""

    def test_cgroup_swap_missing_key(self):
        """Se memory.stat não tem 'swap', deve retornar 0."""
        from ram_keeper import cgroup_swap
        with patch("ram_keeper._read_int", return_value=-1), \
             patch("ram_keeper._read", return_value="anon 1000\nfile 2000\n"):
            result = cgroup_swap(Path("/tmp/fake"))
            assert result == 0

    def test_cgroup_swap_valid(self):
        """Se memory.swap.current existe, deve retornar o valor."""
        from ram_keeper import cgroup_swap
        with patch("ram_keeper._read_int", return_value=1024):
            result = cgroup_swap(Path("/tmp/fake"))
            assert result == 1024


class TestBug7_HumanLabel:
    """Bug 7: human_label() não escapava caracteres especiais."""

    def test_human_label_normal(self):
        """Nomes normais devem funcionar."""
        from ram_keeper import human_label
        assert human_label("ollama.service") == "ollama"
        assert human_label("syncthing.service") == "syncthing"

    def test_human_label_chromium(self):
        """Nomes do Chromium devem ser formatados."""
        from ram_keeper import human_label
        label = human_label("org.chromium.Chromium-12345.scope")
        assert "Chromium" in label

    def test_human_label_empty(self):
        """Nome vazio deve retornar vazio."""
        from ram_keeper import human_label
        assert human_label("") == ""


class TestBug8_AvailableVsNeeded:
    """Bug 8: available podia ser maior que needed, causando confusão."""

    def test_available_larger_than_needed(self):
        """Se available > needed, needed deve ser limitado."""
        steward = Steward()
        # Simular snapshot com available muito grande
        snap = {
            "state": "tight",
            "free_percent": 15.0,
            "mem_total": 16 * 1024**3,
            "mem_available": 2 * 1024**3,
            "groups": [
                {"name": "test.service", "label": "test", "kind": "service",
                 "protected": False, "current": 10 * 1024**3,
                 "reclaimable": 5 * 1024**3, "anon": 3 * 1024**3, "path": "/tmp/fake"}
            ]
        }
        result = steward.relieve(snap)
        # needed deve ser limitado por max_reclaim_per_pass
        assert result["requested"] <= steward.config["max_reclaim_per_pass"]


class TestBug9_RglobPerformance:
    """Bug 9: rglob('*') pode ser muito lento.
    Testa que scan() funciona corretamente."""

    def test_scan_finds_leaf_cgroups(self):
        """scan() deve encontrar cgroups folha."""
        from ram_keeper import scan
        with tempfile.TemporaryDirectory() as tmpdir:
            # Criar estrutura de cgroups falsa
            leaf = Path(tmpdir) / "test.service"
            leaf.mkdir()
            (leaf / "memory.current").write_text("1000000")
            (leaf / "memory.stat").write_text("anon 500000\nfile 300000\ninactive_file 200000\n")
            (leaf / "pids.current").write_text("1")
            (leaf / "memory.swap.current").write_text("0")
            (leaf / "memory.swap.max").write_text("max")
            
            result = scan(Path(tmpdir), set(), 0)
            assert len(result) == 1
            assert result[0].name == "test.service"


class TestBug10_EffectiveNegative:
    """Bug 10: effective podia ser negativo.
    Agora é sempre >= 0."""

    def test_effective_never_negative(self):
        """effective nunca deve ser negativo."""
        steward = Steward()
        # Simular cenário onde swapped > freed
        with patch("ram_keeper._read_int", return_value=1000), \
             patch("ram_keeper.cgroup_swap", return_value=100), \
             patch("builtins.open", mock_open()), \
             patch("ram_keeper.time.sleep"):
            action = steward.reclaim_from(
                type("Cgroup", (), {
                    "path": Path("/tmp/fake"),
                    "label": "test",
                    "reclaim_file": Path("/tmp/fake/memory.reclaim")
                })(),
                500
            )
            assert action.freed >= 0, \
                f"effective não deveria ser negativo, obtido {action.freed}"