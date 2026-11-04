"""
Version des règles appliquées, enregistrée avec chaque décision et chaque trade du journal :
empreinte des réglages de stratégie, de risque et de sortie (8 caractères) + commit git du code
s'il est disponible. Deux trades de même version ont été décidés avec exactement les mêmes règles.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict
from functools import lru_cache

from config.settings import BASE_DIR, settings


@lru_cache(maxsize=1)
def code_version() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=BASE_DIR, capture_output=True, text=True,
                             timeout=5, check=False)
        return out.stdout.strip() or "inconnu"
    except (OSError, subprocess.SubprocessError):
        return "inconnu"


def settings_fingerprint() -> str:
    rules = {"strategy": asdict(settings.strategy), "risk": asdict(settings.risk), "exits": asdict(settings.exits),
             "timeframe": settings.trading.timeframe}
    return hashlib.sha256(json.dumps(rules, sort_keys=True, default=str).encode()).hexdigest()[:8]


def rules_version() -> str:
    return f"règles {settings_fingerprint()} · code {code_version()}"
