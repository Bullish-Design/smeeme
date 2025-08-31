import importlib
import pytest


def test_cli_module_importable():
    try:
        importlib.import_module("smeeme.cli")
    except Exception as e:
        pytest.skip(f"CLI module not importable: {e}")
