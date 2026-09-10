from src.config import Config


def test_config_load_defaults():
    cfg = Config.load()
    assert cfg.db.driver in ("sqlite", "postgres")
    assert cfg.browser.endpoint == ""
    assert cfg.browser.timeout_ms == 30000
