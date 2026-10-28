"""Configuration du bot : `from config import settings`."""

from config.env import BASE_DIR, ConfigError
from config.settings import Settings, settings

__all__ = ["BASE_DIR", "ConfigError", "Settings", "settings"]
