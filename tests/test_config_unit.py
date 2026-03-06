import importlib
import sys

import pytest


def test_config_import_requires_admin_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    sys.modules.pop("app.config", None)

    with pytest.raises(SystemExit, match="ADMIN_TOKEN is not set"):
        importlib.import_module("app.config")