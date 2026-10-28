"""
Stockage persistant SQLite : webhooks reçus, articles analysés, décisions du moteur.

Une connexion unique partagée entre threads (moteur, API), protégée par un verrou,
en mode WAL (les lectures ne bloquent pas les écritures). Toutes les méthodes sont
défensives : une erreur SQLite est loguée et se traduit par une valeur vide
(None, [], 0) ; elle ne remonte jamais jusqu'au moteur de trading.

Tables :
  - webhooks_log     : signaux entrants (acceptés et refusés), payload JSON masqué
  - market_sentiment : articles scrapés (un enregistrement par lien) et leur analyse
  - trade_decisions  : décision du moteur pour chaque symbole à chaque tour

Usage :
    from utils.database import get_database

    db = get_database()
    db.insert_decision({"symbol": "EURUSD", "status": "hold", ...})
    items, total = db.list_decisions(symbol="EURUSD", limit=50)
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from config.settings import settings
from utils.logger import get_logger

log = get_logger(__name__)

SCHEMA_VERSION = 1
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 500
MEMORY = ":memory:"

SCHEMA = """
CREATE TABLE IF NOT EXISTS webhooks_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT    NOT NULL,              -- ISO 8601 UTC
    source      TEXT    NOT NULL,
    symbol      TEXT,
    action      TEXT,
    accepted    INTEGER NOT NULL,              -- 0 / 1
    reason      TEXT    NOT NULL,
    client      TEXT    NOT NULL,
    size_bytes  INTEGER NOT NULL,
    payload     TEXT    NOT NULL               -- JSON, secrets masqués
);
CREATE INDEX IF NOT EXISTS idx_webhooks_received ON webhooks_log (received_at);

CREATE TABLE IF NOT EXISTS market_sentiment (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    link          TEXT    NOT NULL UNIQUE,
    title         TEXT    NOT NULL,
    source        TEXT    NOT NULL,
    published_at  TEXT,
    first_seen_at TEXT    NOT NULL,
    last_seen_at  TEXT    NOT NULL,
    snippet       TEXT    NOT NULL DEFAULT '',
    score         REAL    NOT NULL,
    label         TEXT    NOT NULL,
    keywords      TEXT    NOT NULL DEFAULT '[]',   -- JSON : termes détectés
    symbols       TEXT    NOT NULL DEFAULT '{}'    -- JSON : symbole -> sens de l'impact
);
CREATE INDEX IF NOT EXISTS idx_sentiment_seen ON market_sentiment (last_seen_at);

CREATE TABLE IF NOT EXISTS trade_decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    decided_at      TEXT    NOT NULL,
    cycle           INTEGER,
    symbol          TEXT    NOT NULL,
    status          TEXT    NOT NULL,          -- traded, hold, news_blocked, cooldown, rejected, failed, error
    action          TEXT,                      -- BUY / SELL / HOLD
    confidence      INTEGER,
    technical_score REAL,
    sentiment_score REAL,
    hybrid_score    REAL,
    entry_price     REAL,
    sl              REAL,
    tp              REAL,
    volume          REAL,
    ticket          INTEGER,
    dry_run         INTEGER,
    detail          TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_decisions_time ON trade_decisions (decided_at);
CREATE INDEX IF NOT EXISTS idx_decisions_symbol ON trade_decisions (symbol, decided_at);
"""

WEBHOOK_COLUMNS = ("received_at", "source", "symbol", "action", "accepted", "reason", "client", "size_bytes", "payload")
ARTICLE_COLUMNS = ("link", "title", "source", "published_at", "snippet", "score", "label", "keywords", "symbols")
DECISION_COLUMNS = ("decided_at", "cycle", "symbol", "status", "action", "confidence", "technical_score",
                    "sentiment_score", "hybrid_score", "entry_price", "sl", "tp", "volume", "ticket", "dry_run", "detail")
PURGE_TABLES = {"webhooks_log": "received_at", "market_sentiment": "last_seen_at", "trade_decisions": "decided_at"}


# =============================================================================
#  Helpers
# =============================================================================

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso(value: datetime | str | None) -> str | None:
    """Datetime -> ISO 8601 UTC ; chaîne conservée telle quelle ; None conservé."""
    if value is None or isinstance(value, str):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except ValueError:
        return default


def _page(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(int(limit), MAX_PAGE_SIZE)), max(0, int(offset))


# =============================================================================
#  Base de données
# =============================================================================

class Database:
    """Accès SQLite thread-safe ; `available` vaut False si la base n'a pas pu être ouverte."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self.available = self._open()

    # ------------------------------------------------------------------ connexion

    def _open(self) -> bool:
        try:
            if self.path != MEMORY:
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, check_same_thread=False, timeout=5)
            conn.row_factory = sqlite3.Row
            if self.path != MEMORY:
                conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
            self._conn = conn
            log.info("Base SQLite prête : %s", self.path)
            return True
        except (sqlite3.Error, OSError) as exc:
            log.error("Base SQLite indisponible (%s) : historique désactivé", exc)
            return False

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None
                self.available = False

    def _write(self, sql: str, params: Iterable[Any] = (), many: bool = False) -> sqlite3.Cursor | None:
        """Écriture dans une transaction (validée, ou annulée en cas d'erreur)."""
        if self._conn is None:
            return None
        try:
            with self._lock, self._conn:
                return self._conn.executemany(sql, params) if many else self._conn.execute(sql, tuple(params))
        except sqlite3.Error as exc:
            log.error("Écriture SQLite échouée : %s", exc)
            return None

    def _read(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        if self._conn is None:
            return []
        try:
            with self._lock:
                return self._conn.execute(sql, tuple(params)).fetchall()
        except sqlite3.Error as exc:
            log.error("Lecture SQLite échouée : %s", exc)
            return []

    def _paginate(self, table: str, where: list[str], params: list[Any], order: str,
                  limit: int, offset: int) -> tuple[list[sqlite3.Row], int]:
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        size, start = _page(limit, offset)
        rows = self._read(f"SELECT * FROM {table}{clause} ORDER BY {order} LIMIT ? OFFSET ?", [*params, size, start])
        total_rows = self._read(f"SELECT COUNT(*) AS n FROM {table}{clause}", params)
        return rows, (total_rows[0]["n"] if total_rows else 0)

    # ------------------------------------------------------------------ webhooks_log

    def insert_webhook(self, *, received_at: datetime | str | None = None, source: str, symbol: str | None,
                       action: str | None, accepted: bool, reason: str, client: str, size_bytes: int,
                       payload: Any) -> int | None:
        """Enregistre un webhook ; retourne son identifiant, ou None en cas d'erreur."""
        values = (_iso(received_at) or _now(), source, symbol, action, int(bool(accepted)), reason, client,
                  int(size_bytes), _dumps(payload))
        cursor = self._write(f"INSERT INTO webhooks_log ({', '.join(WEBHOOK_COLUMNS)}) "
                             f"VALUES ({', '.join('?' * len(WEBHOOK_COLUMNS))})", values)
        return cursor.lastrowid if cursor else None

    @staticmethod
    def _webhook_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["accepted"] = bool(item["accepted"])
        item["payload"] = _loads(item["payload"], None)
        return item

    def get_webhook(self, webhook_id: int) -> dict[str, Any] | None:
        rows = self._read("SELECT * FROM webhooks_log WHERE id = ?", [webhook_id])
        return self._webhook_row(rows[0]) if rows else None

    def list_webhooks(self, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0, source: str | None = None,
                      accepted: bool | None = None) -> tuple[list[dict[str, Any]], int]:
        """(webhooks du plus récent au plus ancien, nombre total correspondant aux filtres)."""
        where, params = [], []
        if source:
            where.append("source = ?")
            params.append(source)
        if accepted is not None:
            where.append("accepted = ?")
            params.append(int(accepted))
        rows, total = self._paginate("webhooks_log", where, params, "id DESC", limit, offset)
        return [self._webhook_row(r) for r in rows], total

    def delete_webhook(self, webhook_id: int) -> bool:
        cursor = self._write("DELETE FROM webhooks_log WHERE id = ?", [webhook_id])
        return bool(cursor and cursor.rowcount)

    # ------------------------------------------------------------------ market_sentiment

    def upsert_articles(self, articles: Iterable[Mapping[str, Any]]) -> int:
        """
        Insère les nouveaux articles et met à jour ceux déjà vus (même lien) : dernière
        apparition, score et analyse. Retourne le nombre d'articles traités.
        """
        now = _now()
        rows = [
            (a["link"], a["title"], a.get("source", ""), _iso(a.get("published_at")), a.get("snippet", "") or "",
             float(a["score"]), a["label"], _dumps(a.get("keywords", [])), _dumps(a.get("symbols", {})), now, now)
            for a in articles if a.get("link")
        ]
        if not rows:
            return 0
        cursor = self._write(
            f"INSERT INTO market_sentiment ({', '.join(ARTICLE_COLUMNS)}, first_seen_at, last_seen_at) "
            f"VALUES ({', '.join('?' * (len(ARTICLE_COLUMNS) + 2))}) "
            "ON CONFLICT(link) DO UPDATE SET last_seen_at = excluded.last_seen_at, score = excluded.score, "
            "label = excluded.label, keywords = excluded.keywords, symbols = excluded.symbols, "
            "title = excluded.title, snippet = excluded.snippet",
            rows, many=True,
        )
        return len(rows) if cursor else 0

    @staticmethod
    def _article_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["keywords"] = _loads(item["keywords"], [])
        item["symbols"] = _loads(item["symbols"], {})
        return item

    def list_articles(self, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0, symbol: str | None = None,
                      label: str | None = None) -> tuple[list[dict[str, Any]], int]:
        """(articles du plus récent au plus ancien, total), filtrables par symbole concerné et label."""
        where, params = [], []
        if symbol:
            # json_each : les noms de symboles peuvent contenir des points (ex : US30.cash)
            where.append("EXISTS (SELECT 1 FROM json_each(market_sentiment.symbols) WHERE key = ?)")
            params.append(symbol)
        if label:
            where.append("label = ?")
            params.append(label)
        rows, total = self._paginate("market_sentiment", where, params,
                                     "COALESCE(published_at, first_seen_at) DESC, id DESC", limit, offset)
        return [self._article_row(r) for r in rows], total

    # ------------------------------------------------------------------ trade_decisions

    def insert_decision(self, record: Mapping[str, Any]) -> int | None:
        """Enregistre une décision du moteur (colonnes absentes = NULL) ; retourne son identifiant."""
        values = []
        for column in DECISION_COLUMNS:
            value = record.get(column)
            if column == "decided_at":
                value = _iso(value) or _now()
            elif column == "dry_run" and value is not None:
                value = int(bool(value))
            elif column == "detail":
                value = str(value or "")
            values.append(value)
        cursor = self._write(f"INSERT INTO trade_decisions ({', '.join(DECISION_COLUMNS)}) "
                             f"VALUES ({', '.join('?' * len(DECISION_COLUMNS))})", values)
        return cursor.lastrowid if cursor else None

    @staticmethod
    def _decision_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["dry_run"] = None if item["dry_run"] is None else bool(item["dry_run"])
        return item

    def list_decisions(self, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0, symbol: str | None = None,
                       status: str | Iterable[str] | None = None,
                       since: datetime | str | None = None) -> tuple[list[dict[str, Any]], int]:
        """(décisions de la plus récente à la plus ancienne, total), filtrables par symbole, statut(s), date."""
        where, params = [], []
        if symbol:
            where.append("symbol = ?")
            params.append(symbol)
        if status:
            statuses = [status] if isinstance(status, str) else list(status)
            where.append(f"status IN ({', '.join('?' * len(statuses))})")
            params.extend(statuses)
        if since:
            where.append("decided_at >= ?")
            params.append(_iso(since))
        rows, total = self._paginate("trade_decisions", where, params, "id DESC", limit, offset)
        return [self._decision_row(r) for r in rows], total

    # ------------------------------------------------------------------ maintenance

    def purge_older_than(self, days: int) -> dict[str, int]:
        """Supprime l'historique plus ancien que `days` jours ; retourne le nombre de lignes par table."""
        cutoff = _iso(datetime.now(timezone.utc) - timedelta(days=days))
        deleted: dict[str, int] = {}
        for table, column in PURGE_TABLES.items():
            cursor = self._write(f"DELETE FROM {table} WHERE {column} < ?", [cutoff])
            deleted[table] = cursor.rowcount if cursor else 0
        if any(deleted.values()):
            log.info("Historique purgé (> %d jours) : %s", days, deleted)
        return deleted


# =============================================================================
#  Instance partagée
# =============================================================================

_database: Database | None = None
_database_lock = threading.Lock()


def get_database() -> Database:
    """Base partagée, ouverte au premier appel à l'emplacement `settings.database.path`."""
    global _database
    with _database_lock:
        if _database is None:
            _database = Database(settings.database.path)
        return _database
