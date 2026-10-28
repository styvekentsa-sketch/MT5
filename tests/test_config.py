"""Validation de la configuration."""

import pytest

from config import ConfigError, settings
from config.settings import ApiConfig


def test_settings_loaded_from_test_environment():
    assert settings.trading.symbols == ("EURUSD", "XAUUSD")
    assert settings.dry_run is True
    assert abs(settings.strategy.technical_weight + settings.strategy.sentiment_weight - 1.0) < 1e-9


def _api(**overrides) -> ApiConfig:
    values = dict(enabled=True, host="127.0.0.1", port=8000, api_key="x" * 32,
                  cors_origins=("http://localhost:5173",))
    values.update(overrides)
    return ApiConfig(**values)


def test_api_config_valid():
    _api().validate()
    assert _api().is_local_only
    assert not _api(host="0.0.0.0").is_local_only


@pytest.mark.parametrize("overrides", [
    {"api_key": "trop-courte"},
    {"api_key": ""},
    {"cors_origins": ("*",)},
    {"port": 70_000},
])
def test_api_config_rejects_unsafe_values(overrides):
    with pytest.raises(ConfigError):
        _api(**overrides).validate()


def test_api_config_disabled_skips_checks():
    _api(enabled=False, api_key="").validate()
