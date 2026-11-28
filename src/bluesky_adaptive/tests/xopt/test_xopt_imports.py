import importlib
import sys

import pytest


def test_xopt_import_names_the_xopt_extra_when_xopt_is_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "xopt", None)
    for name in [name for name in sys.modules if name.startswith("bluesky_adaptive.xopt")]:
        monkeypatch.delitem(sys.modules, name)

    with pytest.raises(ImportError, match=r"pip install bluesky-adaptive\[xopt\]"):
        importlib.import_module("bluesky_adaptive.xopt")
