import importlib
import sys

import pytest


def test_config_import_tolerates_missing_admin_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    sys.modules.pop("app.config", None)

    module = importlib.import_module("app.config")

    assert module.settings.admin_token == ""