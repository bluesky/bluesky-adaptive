import importlib
import sys

import pytest


def test_scipy_import_names_the_scipy_extra_when_scipy_is_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "scipy.optimize", None)
    for name in [name for name in sys.modules if name.startswith("bluesky_adaptive.scipy")]:
        monkeypatch.delitem(sys.modules, name)

    with pytest.raises(ImportError, match=r"pip install bluesky-adaptive\[scipy\]"):
        importlib.import_module("bluesky_adaptive.scipy")
