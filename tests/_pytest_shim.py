"""Minimal stdlib-only pytest shim for environments without pytest installed.

When real pytest is importable (Linux CI, developer machines), `pytest` is the
genuine module and `install_unittest_bridge()` is a complete no-op so pytest
collection is unaffected (regression SO-1).

When pytest is absent (e.g. stdlib `python -m unittest discover`), this module
supplies the subset of pytest used by the suite (`fixture`, `mark.parametrize`,
`mark.skipif`, `raises`, `skip`) and synthesizes a `unittest.TestCase` subclass
with per-test isolation (`RAM_KEEPER_CONFIG`, `RAM_KEEPER_STATE_DIR`, `tmp_path`
and `monkeypatch`).
"""
from __future__ import annotations

import importlib
import inspect
import itertools
import os
import re
import tempfile
import unittest
from pathlib import Path

try:
    import pytest as _real_pytest  # type: ignore[import-not-found]
    pytest = _real_pytest
    USING_PYTEST_SHIM = False
except ImportError:
    USING_PYTEST_SHIM = True

    _MISSING = object()

    def _resolve_dotted(dotted: str):
        parts = dotted.split(".")
        if len(parts) < 2:
            raise TypeError(f"must be 'module.attr', got {dotted!r}")
        mod = None
        attr_idx = 0
        for idx in range(len(parts) - 1, 0, -1):
            try:
                mod = importlib.import_module(".".join(parts[:idx]))
                attr_idx = idx
                break
            except ImportError:
                continue
        if mod is None:
            raise ImportError(f"could not import module from {dotted!r}")
        obj = mod
        for part in parts[attr_idx:-1]:
            obj = getattr(obj, part)
        return obj, parts[-1]

    class _MonkeyPatch:
        def __init__(self) -> None:
            self._env_undo: list[tuple[str, object]] = []
            self._attr_undo: list[tuple[object, str, object]] = []

        def setenv(self, name: str, value: str, prepend: str | None = None) -> None:
            key = str(name)
            old = os.environ.get(key, _MISSING)
            self._env_undo.append((key, old))
            val = str(value)
            if prepend is not None and old is not _MISSING:
                val = f"{val}{prepend}{old}"
            os.environ[key] = val

        def delenv(self, name: str, raising: bool = True) -> None:
            key = str(name)
            if key not in os.environ:
                if raising:
                    raise KeyError(key)
                return
            self._env_undo.append((key, os.environ[key]))
            del os.environ[key]

        def setattr(self, target, name, value=_MISSING, raising: bool = True) -> None:
            if value is _MISSING and isinstance(target, str):
                obj, attr_name = _resolve_dotted(target)
                val = name
            else:
                obj, attr_name, val = target, name, value
            if isinstance(obj, type) and attr_name in obj.__dict__:
                old = obj.__dict__[attr_name]
            else:
                old = getattr(obj, attr_name, _MISSING)
            if old is _MISSING and raising:
                raise AttributeError(f"{obj!r} has no attribute {attr_name!r}")
            self._attr_undo.append((obj, attr_name, old))
            setattr(obj, attr_name, val)

        def delattr(self, target, name=_MISSING, raising: bool = True) -> None:
            if name is _MISSING and isinstance(target, str):
                obj, attr_name = _resolve_dotted(target)
            else:
                obj, attr_name = target, name
            if isinstance(obj, type) and attr_name in obj.__dict__:
                old = obj.__dict__[attr_name]
            else:
                old = getattr(obj, attr_name, _MISSING)
            if old is _MISSING:
                if raising:
                    raise AttributeError(f"{obj!r} has no attribute {attr_name!r}")
                return
            self._attr_undo.append((obj, attr_name, old))
            delattr(obj, attr_name)

        def undo(self) -> None:
            for obj, attr_name, old in reversed(self._attr_undo):
                if old is _MISSING:
                    try:
                        delattr(obj, attr_name)
                    except AttributeError:
                        pass
                else:
                    setattr(obj, attr_name, old)
            self._attr_undo.clear()
            for key, old in reversed(self._env_undo):
                if old is _MISSING:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = str(old)
            self._env_undo.clear()

    class _RaisesCtx:
        def __init__(self, expected, match: str | None = None) -> None:
            self.expected = expected
            self.match = match
            self.value = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, _tb) -> bool:
            if exc_type is None:
                raise AssertionError(f"DID NOT RAISE {self.expected!r}")
            if not issubclass(exc_type, self.expected):
                return False
            self.value = exc_val
            if self.match is not None and not re.search(self.match, str(exc_val)):
                raise AssertionError(
                    f"Regex pattern {self.match!r} does not match {str(exc_val)!r}"
                )
            return True

    class _Mark:
        @staticmethod
        def skipif(cond: bool, reason: str = ""):
            def deco(fn):
                if cond:
                    fn._shim_skip_reason = reason or "skipped"
                return fn
            return deco

        @staticmethod
        def parametrize(argnames, argvalues, **_kwargs):
            if isinstance(argnames, str):
                names = [a.strip() for a in argnames.split(",") if a.strip()]
            else:
                names = list(argnames)
            vals = list(argvalues)

            def deco(fn):
                existing = list(getattr(fn, "_shim_params", []))
                existing.append((names, vals))
                fn._shim_params = existing
                return fn
            return deco

    class _PytestShim:
        mark = _Mark()

        @staticmethod
        def fixture(fn=None, *, autouse: bool = False, **_kwargs):
            def deco(f):
                f._is_pytest_fixture = True
                f._autouse = autouse
                return f
            return deco(fn) if fn is not None else deco

        @staticmethod
        def skip(reason: str = "") -> None:
            raise unittest.SkipTest(reason or "skipped")

        @staticmethod
        def raises(expected, match: str | None = None) -> _RaisesCtx:
            return _RaisesCtx(expected, match=match)

    pytest = _PytestShim()  # type: ignore[assignment]


def _expand_params(param_specs: list[tuple[list[str], list]]) -> list[dict[str, object]]:
    if not param_specs:
        return [{}]
    per_spec: list[list[dict[str, object]]] = []
    for names, values in reversed(param_specs):
        rows: list[dict[str, object]] = []
        for item in values:
            if len(names) == 1:
                rows.append({names[0]: item})
            else:
                seq = list(item)
                rows.append(dict(zip(names, seq)))
        per_spec.append(rows)
    out: list[dict[str, object]] = []
    for combo in itertools.product(*per_spec):
        merged: dict[str, object] = {}
        for d in combo:
            merged.update(d)
        out.append(merged)
    return out


def install_unittest_bridge(ns: dict[str, object]):
    """Populate `ns['_PytestUnittestBridge']` ONLY when real pytest is absent."""
    if not USING_PYTEST_SHIM:
        return None

    fixtures: dict[str, object] = {}
    autouse_fixtures: list[str] = []
    items: list[tuple[str, type | None, str, object]] = []

    for name, obj in list(ns.items()):
        if callable(obj) and getattr(obj, "_is_pytest_fixture", False):
            fixtures[name] = obj
            if getattr(obj, "_autouse", False):
                autouse_fixtures.append(name)
        elif inspect.isfunction(obj) and name.startswith("test_"):
            items.append((name, None, name, obj))
        elif (
            isinstance(obj, type)
            and name.startswith("Test")
            and not issubclass(obj, unittest.TestCase)
        ):
            for attr in dir(obj):
                if not attr.startswith("test_"):
                    continue
                raw = inspect.getattr_static(obj, attr)
                fn = raw.__func__ if isinstance(raw, (staticmethod, classmethod)) else raw
                if callable(fn):
                    items.append((f"{name}__{attr}", obj, attr, fn))

    methods: dict[str, object] = {}

    def _make_method(cls_ref, attr_name: str, raw_fn, param_row: dict[str, object]):
        skip_reason = getattr(raw_fn, "_shim_skip_reason", None)

        def _test(self):
            if skip_reason is not None:
                raise unittest.SkipTest(skip_reason)
            mp = _MonkeyPatch()
            teardowns: list = []
            with tempfile.TemporaryDirectory(prefix="rk-ut-") as td:
                tmp_path = Path(td)
                cfg_dir = tmp_path / "ram-keeper"
                mp.setenv("RAM_KEEPER_CONFIG", str(cfg_dir / "config.json"))
                mp.setenv("RAM_KEEPER_STATE_DIR", str(tmp_path / "rk-state"))
                cache: dict[str, object] = {
                    "tmp_path": tmp_path,
                    "monkeypatch": mp,
                }

                def resolve(fname: str):
                    if fname in cache:
                        return cache[fname]
                    if fname not in fixtures:
                        raise KeyError(fname)
                    ffn = fixtures[fname]
                    fsig = inspect.signature(ffn)
                    fkwargs = {p: resolve(p) for p in fsig.parameters}
                    val = ffn(**fkwargs)
                    if inspect.isgenerator(val):
                        res = next(val)
                        teardowns.append(val)
                    else:
                        res = val
                    cache[fname] = res
                    return res

                try:
                    for af in autouse_fixtures:
                        resolve(af)
                    if cls_ref is not None:
                        inst = cls_ref()
                        target = getattr(inst, attr_name)
                    else:
                        target = raw_fn
                    sig = inspect.signature(target)
                    call_kwargs: dict[str, object] = {}
                    for pname, param in sig.parameters.items():
                        if pname in param_row:
                            call_kwargs[pname] = param_row[pname]
                        elif pname in cache or pname in fixtures:
                            call_kwargs[pname] = resolve(pname)
                        elif param.default is not inspect.Parameter.empty:
                            continue
                        else:
                            raise TypeError(
                                f"Unresolved fixture {pname!r} for {attr_name}"
                            )
                    target(**call_kwargs)
                finally:
                    for gen in reversed(teardowns):
                        try:
                            next(gen)
                        except StopIteration:
                            pass
                    mp.undo()

        return _test

    for base_label, cls_ref, attr_name, fn in items:
        rows = _expand_params(list(getattr(fn, "_shim_params", [])))
        if len(rows) == 1 and not rows[0]:
            mname = f"test__{base_label}"
            methods[mname] = _make_method(cls_ref, attr_name, fn, {})
        else:
            for idx, row in enumerate(rows):
                mname = f"test__{base_label}__{idx}"
                methods[mname] = _make_method(cls_ref, attr_name, fn, row)

    bridge = type("_PytestUnittestBridge", (unittest.TestCase,), methods)
    ns["_PytestUnittestBridge"] = bridge
    return bridge
