"""Opérations CRUD de la base SQLite et robustesse."""

from datetime import datetime, timedelta, timezone

from utils.database import MAX_PAGE_SIZE, SCHEMA_VERSION, Database

NOW = datetime.now(timezone.utc)


def _webhook(db, **overrides):
    fields = dict(source="tradingview", symbol="EURUSD", action="BUY", accepted=True, reason="ok",
                  client="1.2.3.4", size_bytes=42, payload={"price": 1.1, "passphrase": "***"})
    fields.update(overrides)
    return db.insert_webhook(**fields)


def _article(link="https://ex.com/a", **overrides):
    article = dict(link=link, title="Dollar surges", source="Reuters", published_at=NOW, snippet="",
                   score=0.5, label="bullish", keywords=["surges"], symbols={"EURUSD": -1.0, "US30.cash": 1.0})
    article.update(overrides)
    return article


# ----------------------------------------------------------------- schéma

def test_schema_and_version_created(tmp_path):
    db = Database(tmp_path / "sub" / "bot.db")
    assert db.available and (tmp_path / "sub" / "bot.db").exists()
    tables = {r["name"] for r in db._read("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"webhooks_log", "market_sentiment", "trade_decisions"} <= tables
    assert db._read("PRAGMA user_version")[0][0] == SCHEMA_VERSION
    db.close()


def test_data_persists_across_connections(tmp_path):
    path = tmp_path / "bot.db"
    first = Database(path)
    _webhook(first)
    first.close()
    second = Database(path)
    assert second.list_webhooks()[1] == 1
    second.close()


# ----------------------------------------------------------------- webhooks_log

def test_webhook_create_read_delete(isolated_database):
    db = isolated_database
    webhook_id = _webhook(db)
    item = db.get_webhook(webhook_id)
    assert item["source"] == "tradingview" and item["accepted"] is True
    assert item["payload"] == {"price": 1.1, "passphrase": "***"}
    assert db.delete_webhook(webhook_id) is True
    assert db.get_webhook(webhook_id) is None and db.delete_webhook(webhook_id) is False


def test_webhook_text_payload_and_filters(isolated_database):
    db = isolated_database
    _webhook(db, payload="BUY EURUSD", source="custom")
    _webhook(db, accepted=False, reason="secret invalide")
    _webhook(db)
    items, total = db.list_webhooks()
    assert total == 3 and [i["id"] for i in items] == [3, 2, 1]          # plus récent d'abord
    assert db.list_webhooks(source="custom")[0][0]["payload"] == "BUY EURUSD"
    rejected, count = db.list_webhooks(accepted=False)
    assert count == 1 and rejected[0]["reason"] == "secret invalide"


def test_pagination(isolated_database):
    db = isolated_database
    for _ in range(7):
        _webhook(db)
    page, total = db.list_webhooks(limit=3, offset=3)
    assert total == 7 and [i["id"] for i in page] == [4, 3, 2]
    assert len(db.list_webhooks(limit=10_000)[0]) == 7                    # limite plafonnée sans erreur
    assert len(db.list_webhooks(limit=0)[0]) == 1 and MAX_PAGE_SIZE == 500


# ----------------------------------------------------------------- market_sentiment

def test_article_upsert_inserts_then_updates(isolated_database):
    db = isolated_database
    assert db.upsert_articles([_article(), _article("https://ex.com/b", score=-0.3, label="bearish")]) == 2
    first_seen = db.list_articles()[0][0]["first_seen_at"]
    db.upsert_articles([_article(score=0.9, keywords=["surges", "record high"])])
    items, total = db.list_articles()
    updated = next(i for i in items if i["link"] == "https://ex.com/a")
    assert total == 2 and updated["score"] == 0.9 and updated["keywords"] == ["surges", "record high"]
    assert updated["first_seen_at"] == first_seen                         # première apparition conservée


def test_article_filters_handle_dotted_symbols(isolated_database):
    db = isolated_database
    db.upsert_articles([_article(), _article("https://ex.com/b", label="bearish", symbols={"XAUUSD": 1.0})])
    assert db.list_articles(symbol="US30.cash")[1] == 1
    assert db.list_articles(symbol="XAUUSD")[0][0]["link"] == "https://ex.com/b"
    assert db.list_articles(label="bearish")[1] == 1
    assert db.upsert_articles([{"title": "sans lien"}]) == 0


# ----------------------------------------------------------------- trade_decisions

def test_decision_create_and_filters(isolated_database):
    db = isolated_database
    db.insert_decision({"symbol": "EURUSD", "status": "traded", "action": "BUY", "confidence": 72,
                        "hybrid_score": 0.72, "entry_price": 1.1, "sl": 1.098, "tp": 1.104, "volume": 0.5,
                        "ticket": -1, "dry_run": True, "cycle": 3})
    db.insert_decision({"symbol": "EURUSD", "status": "hold", "detail": "score insuffisant"})
    db.insert_decision({"symbol": "XAUUSD", "status": "news_blocked",
                        "decided_at": NOW - timedelta(days=2)})
    items, total = db.list_decisions()
    assert total == 3 and items[-1]["symbol"] == "EURUSD"
    traded = db.list_decisions(status="traded")[0][0]
    assert traded["dry_run"] is True and traded["volume"] == 0.5 and traded["ticket"] == -1
    assert db.list_decisions(symbol="EURUSD", status=["traded", "hold"])[1] == 2
    assert db.list_decisions(since=NOW - timedelta(days=1))[1] == 2
    assert db.list_decisions(status="hold")[0][0]["dry_run"] is None


def test_purge_older_than(isolated_database):
    db = isolated_database
    old = NOW - timedelta(days=120)
    _webhook(db, received_at=old)
    _webhook(db)
    db.insert_decision({"symbol": "EURUSD", "status": "hold", "decided_at": old})
    deleted = db.purge_older_than(90)
    assert deleted["webhooks_log"] == 1 and deleted["trade_decisions"] == 1
    assert db.list_webhooks()[1] == 1 and db.list_decisions()[1] == 0


# ----------------------------------------------------------------- robustesse

def test_unavailable_database_degrades_gracefully(tmp_path):
    blocker = tmp_path / "fichier"
    blocker.write_text("x")
    db = Database(blocker / "impossible.db")                 # parent = un fichier : ouverture impossible
    assert db.available is False
    assert _webhook(db) is None and db.list_webhooks() == ([], 0)
    assert db.insert_decision({"symbol": "EURUSD", "status": "hold"}) is None
    assert db.upsert_articles([_article()]) == 0 and db.purge_older_than(1) == {
        "webhooks_log": 0, "market_sentiment": 0, "trade_decisions": 0}


def test_sql_errors_never_raise(isolated_database):
    db = isolated_database
    assert db.insert_decision({"symbol": None, "status": "hold"}) is None   # NOT NULL violé
    assert db._read("SELECT * FROM table_inexistante") == []
    db.close()
    assert _webhook(db) is None and db.list_decisions() == ([], 0)


def test_concurrent_writes_are_serialized(isolated_database):
    import threading

    db = isolated_database
    threads = [threading.Thread(target=lambda: [_webhook(db) for _ in range(25)]) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert db.list_webhooks()[1] == 100
