"""Journal des décisions et rejets : verdicts chiffrés, motifs par étape, migration SQLite, route /api/bot/decisions."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from core import engine as eng
from core.engine import TradingEngine
from strategies.strategy_manager import decide
from tests.conftest import TEST_API_KEY
from tests.fakes import FakeBroker
from tests.test_strategy_manager import _tech
from utils import database
from utils.database import Database

AUTH = {"X-API-Key": TEST_API_KEY}


# ----------------------------------------------------------------- verdicts de la stratégie

def test_verdict_below_threshold_quotes_scores():
    result = decide(_tech(score=0.5), sentiment=-0.08, news_blocked=False)   # 0.35 - 0.024 = 0.326
    assert result.action == "HOLD" and result.direction == "BUY"
    assert result.verdict == "Score hybride +0.33 sous le seuil de ±0.60 (technique +0.50, sentiment -0.08)"


def test_verdict_unconfirmed_and_accepted_and_news():
    unconfirmed = decide(_tech(signal="NEUTRAL", score=0.6), sentiment=1.0, news_blocked=False)
    assert unconfirmed.action == "HOLD" and "ne confirme pas le BUY" in unconfirmed.verdict
    accepted = decide(_tech(score=1.0), sentiment=0.5, news_blocked=False)
    assert accepted.action == "BUY" and accepted.verdict.startswith("Signal BUY confirmé : score hybride +0.85 au-delà du seuil de ±0.60")
    blocked = decide(_tech(score=1.0), sentiment=0.5, news_blocked=True)
    assert blocked.action == "HOLD" and "fort impact" in blocked.verdict


# ----------------------------------------------------------------- motifs enregistrés par le moteur

@pytest.fixture
def pipeline(monkeypatch):
    broker = FakeBroker()
    state = {"decision": {"action": "BUY", "direction": "BUY", "confidence": 72, "entry_price": 1.1,
                          "suggested_sl": 1.098, "suggested_tp": 1.104, "reason": "analyse complète",
                          "verdict": "Signal BUY confirmé : score hybride +0.72"},
             "volume": 0.5, "check": {"is_approved": True, "reason": "ok"}}
    monkeypatch.setattr(eng, "is_high_impact_news_imminent", lambda symbol: False)
    monkeypatch.setattr(eng, "evaluate_market", lambda symbol, broker: dict(state["decision"], symbol=symbol))
    monkeypatch.setattr(eng, "calculate_position_size", lambda symbol, entry, sl, broker: state["volume"])
    monkeypatch.setattr(eng, "validate_trade_risk", lambda *a, **k: state["check"])
    return TradingEngine(symbols=["EURUSD"], broker=broker), state, broker


def _last(engine):
    return engine.recent_decisions[-1]


def test_accepted_trade_is_recorded_with_verdict(pipeline, isolated_database):
    engine, _, _ = pipeline
    engine.step()
    record = _last(engine)
    assert record["status"] == "traded" and record["direction"] == "BUY"
    assert record["verdict"].startswith("BUY 0.5 exécuté (simulation PAPER) · ticket #-1 · Signal BUY confirmé")
    [row], _ = isolated_database.list_decisions()
    assert row["verdict"] == record["verdict"] and row["direction"] == "BUY"


@pytest.mark.parametrize("change, status, expected", [
    ({"check": {"is_approved": False, "reason": "spread 40 pts > maximum 30 pts"}}, "rejected",
     "Risque : spread 40 pts > maximum 30 pts"),
    ({"check": {"is_approved": False, "reason": "3 positions ouvertes (maximum 3)"}}, "rejected",
     "Risque : 3 positions ouvertes (maximum 3)"),
    ({"volume": 0.0}, "rejected", "Taille de position nulle"),
    ({"decision": {"action": "HOLD", "direction": "SELL", "reason": "long",
                   "verdict": "Score hybride -0.42 sous le seuil de ±0.60"}}, "hold",
     "Score hybride -0.42 sous le seuil de ±0.60"),
])
def test_rejections_keep_precise_reason(pipeline, change, status, expected):
    engine, state, _ = pipeline
    state.update(change)
    engine.step()
    record = _last(engine)
    assert record["status"] == status and record["verdict"].startswith(expected)


def test_broker_refusal_and_cooldown(pipeline):
    engine, _, broker = pipeline
    engine.trade_cooldown_seconds = 900
    engine.step()
    engine.step()
    assert _last(engine)["status"] == "cooldown" and "déjà été ouvert sur cette bougie" in _last(engine)["verdict"]
    engine._last_trade_at.clear()
    broker.order_ok = False
    engine.step()
    assert _last(engine)["verdict"] == "Ordre refusé par le broker : refusé par le broker"


# ----------------------------------------------------------------- migration d'une base existante

def test_existing_database_gets_new_columns(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE trade_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, decided_at TEXT NOT NULL, "
                 "cycle INTEGER, symbol TEXT NOT NULL, status TEXT NOT NULL, action TEXT, confidence INTEGER, "
                 "technical_score REAL, sentiment_score REAL, hybrid_score REAL, entry_price REAL, sl REAL, "
                 "tp REAL, volume REAL, ticket INTEGER, dry_run INTEGER, detail TEXT NOT NULL DEFAULT '')")
    conn.execute("INSERT INTO trade_decisions (decided_at, symbol, status, detail) VALUES ('2026-10-28', 'EURUSD', 'hold', 'ancien')")
    conn.commit()
    conn.close()

    db = Database(path)
    [old], _ = db.list_decisions()
    assert old["detail"] == "ancien" and old["verdict"] == "" and old["direction"] is None
    db.insert_decision({"symbol": "EURUSD", "status": "hold", "direction": "SELL", "verdict": "seuil"})
    assert db.list_decisions()[0][0]["verdict"] == "seuil"
    db.close()


# ----------------------------------------------------------------- route /api/bot/decisions

@pytest.fixture
def client(pipeline):
    engine, state, _ = pipeline
    engine.step()                                                   # accepté
    state["check"] = {"is_approved": False, "reason": "spread 40 pts > maximum 30 pts"}
    engine._last_trade_at.clear()
    engine.step()                                                   # rejeté
    return TestClient(create_app(engine))


def test_decisions_route_normalises_items(client):
    body = client.get("/api/bot/decisions", headers=AUTH).json()
    assert body["source"] == "sqlite" and (body["accepted"], body["rejected"]) == (1, 1)
    rejected, accepted = body["items"]                              # du plus récent au plus ancien
    assert rejected["status"] == "REJETÉ" and rejected["action"] == "ACHAT" and rejected["stage"] == "risque"
    assert rejected["reason"] == "Risque : spread 40 pts > maximum 30 pts" and rejected["cycle"] == 2
    assert accepted["status"] == "ACCEPTÉ" and accepted["stage"] == "exécution" and accepted["dry_run"] is True


def test_decisions_route_filters_and_requires_key(client):
    only = client.get("/api/bot/decisions?status=accepted", headers=AUTH).json()
    assert [i["status"] for i in only["items"]] == ["ACCEPTÉ"]
    assert client.get("/api/bot/decisions?symbol=gbpusd", headers=AUTH).json()["items"] == []
    assert client.get("/api/bot/decisions?limit=0", headers=AUTH).status_code == 422
    assert client.get("/api/bot/decisions").status_code == 401


def test_decisions_route_falls_back_to_memory(client, monkeypatch):
    unavailable = Database(database.MEMORY)
    unavailable.available = False
    monkeypatch.setattr(database, "_database", unavailable)
    body = client.get("/api/bot/decisions?limit=1", headers=AUTH).json()
    assert body["source"] == "memory" and len(body["items"]) == 1 and body["items"][0]["status"] == "REJETÉ"
