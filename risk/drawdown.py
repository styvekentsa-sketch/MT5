"""
Suspension sur drawdown : plus haut des fonds propres suivi par compte (fichier JSON, survit aux
redémarrages) ; au-delà de RISK_MAX_DRAWDOWN_PCT sous ce plus haut, plus aucune nouvelle entrée.
Les positions ouvertes restent gérées (stops, sorties partielles, Bouclier). La suspension se lève
quand les fonds propres repassent sous le seuil, ou par une remise à zéro explicite du plus haut.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from brokers.models import AccountInfo
from config.settings import settings
from utils.logger import get_logger

log = get_logger(__name__)


class DrawdownMonitor:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or settings.database.path.parent / "equity_peaks.json"
        self._lock = threading.Lock()
        self.peaks: dict[str, float] = self._load()
        self.last: dict[str, float] = {}

    def _load(self) -> dict[str, float]:
        try:
            return {k: float(v) for k, v in json.loads(self.path.read_text(encoding="utf-8")).items()}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.warning("Plus hauts des fonds propres illisibles (%s) : suivi repris à zéro", exc)
            return {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.peaks, indent=2), encoding="utf-8")
        except OSError as exc:
            log.warning("Plus haut des fonds propres non enregistré : %s", exc)

    @staticmethod
    def key(account: AccountInfo) -> str:
        return f"{getattr(account, 'server', '') or 'compte'}:{account.login}"

    def update(self, account: AccountInfo) -> float:
        """Drawdown courant en % du plus haut (0 au plus haut)."""
        key = self.key(account)
        with self._lock:
            peak = self.peaks.get(key, 0.0)
            if account.equity > peak:
                self.peaks[key] = peak = account.equity
                self._save()
            drawdown = (peak - account.equity) / peak * 100 if peak > 0 else 0.0
            self.last[key] = drawdown
            return drawdown

    def reset(self, account: AccountInfo) -> None:
        with self._lock:
            self.peaks[self.key(account)] = account.equity
            self._save()

    def check(self, account: AccountInfo | None) -> str | None:
        """Motif de suspension, ou None (limite non atteinte, désactivée ou compte indisponible)."""
        limit = settings.risk.max_drawdown_pct
        if account is None or limit <= 0 or account.equity <= 0:
            return None
        drawdown = self.update(account)
        if drawdown >= limit:
            return (f"drawdown {drawdown:.2f}% depuis le plus haut des fonds propres "
                    f"({self.peaks[self.key(account)]:.2f}) >= limite {limit}% : nouvelles entrées suspendues")
        return None
