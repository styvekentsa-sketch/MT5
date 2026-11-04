"""
Latence mesurée séparément pour chaque étape du traitement (secondes, horloge monotone) :
actualités (fenêtre de sécurité), analyse (données + stratégie), risque (taille, règles,
vérificateur), ordre (transmission et confirmation du broker), tour complet. Fenêtre glissante
des dernières mesures : dernière valeur, moyenne, 95e centile et maximum.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Any, Iterator

WINDOW = 200


class LatencyTracker:
    def __init__(self, window: int = WINDOW) -> None:
        self.window = window
        self._lock = threading.Lock()
        self._samples: dict[str, deque[float]] = {}

    def record(self, stage: str, seconds: float) -> None:
        with self._lock:
            self._samples.setdefault(stage, deque(maxlen=self.window)).append(max(0.0, seconds))

    @contextmanager
    def measure(self, stage: str) -> Iterator[None]:
        started = time.monotonic()
        try:
            yield
        finally:
            self.record(stage, time.monotonic() - started)

    def summary(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            snapshot = {stage: list(values) for stage, values in self._samples.items()}
        out = {}
        for stage, values in snapshot.items():
            ordered = sorted(values)
            out[stage] = {"last_ms": round(values[-1] * 1000, 1), "avg_ms": round(sum(values) / len(values) * 1000, 1),
                          "p95_ms": round(ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))] * 1000, 1),
                          "max_ms": round(ordered[-1] * 1000, 1), "count": len(values)}
        return out
