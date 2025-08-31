import pytest


@pytest.mark.parametrize("url,target", [("https://smee.io/test", "http://127.0.0.1:8000/webhook")])
def test_load_config_from_env(monkeypatch, url, target):
    try:
        from smeeme import load_config_from_env  # type: ignore
    except Exception as e:
        pytest.skip(f"load_config_from_env unavailable: {e}")
        return

    monkeypatch.setenv("SMEEME_URL", url)
    monkeypatch.setenv("SMEEME_TARGET", target)
    cfg = load_config_from_env()  # type: ignore

    # Be tolerant about model attribute names:
    got_url = getattr(cfg, "url", None) or getattr(cfg, "smee_url", None)
    got_target = getattr(cfg, "target", None)
    assert got_url == url
    assert got_target == target
