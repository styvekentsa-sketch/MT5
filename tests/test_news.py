"""
Pipeline des actualités : sources du calendrier, fusion et déduplication, fenêtre de sécurité
(-30 / +15 min), blocage du moteur et des setups, alertes uniques, analyse structurée.
"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from core import engine as eng
from core import news_pipeline as npl
from core.engine import TradingEngine
from core.news_pipeline import (
    CalendarEvent,
    CalendarProvider,
    Importance,
    NewsPipeline,
    SimulatedCalendarProvider,
    analyze_article,
    analyze_event,
    dedupe_articles,
    parse_value,
)
from models.schemas import MarketRegime, SetupStatus, Side, TradingSetup, TradingStyle
from strategies.base import BaseStrategy, BuyPlan
from tests.fakes import FakeBroker

ANNOUNCE = datetime(2026, 11, 6, 13, 30, tzinfo=timezone.utc)                # NFP 13:30 UTC


def _event(title="Non-Farm Employment Change", currency="USD", importance="high", minutes=0, **values):
    return {"time": (ANNOUNCE + timedelta(minutes=minutes)).isoformat(), "title": title, "currency": currency,
            "importance": importance, **values}


def _pipeline(*events, providers=None) -> NewsPipeline:
    providers = providers or [SimulatedCalendarProvider(events)]
    return NewsPipeline(providers, minutes_before=30, minutes_after=15, symbols=["EURUSD", "XAUUSD", "GBPJPY"],
                        cache_seconds=0)


@pytest.fixture(autouse=True)
def _reset_pipeline():
    yield
    npl.set_pipeline(None)


# ----------------------------------------------------------------- sources et événements

def test_simulated_provider_from_file_and_fields(tmp_path):
    path = tmp_path / "calendar.json"
    path.write_text(json.dumps([_event(actual="250K", consensus="180K", previous="150K")]), encoding="utf-8")
    [event] = _pipeline(providers=[SimulatedCalendarProvider(path=path)]).events()
    assert (event.importance, event.actual, event.consensus, event.previous) == (Importance.HIGH, "250K", "180K", "150K")
    assert event.source == "simulé" and event.time == ANNOUNCE and event.assets == ("EURUSD", "XAUUSD")


def test_events_from_several_sources_are_deduplicated_and_completed():
    class Live(CalendarProvider):
        name = "Forex Factory"

        def fetch(self):
            return [CalendarEvent(time=ANNOUNCE, source=self.name, title="Non-Farm Employment Change",
                                  currency="USD", importance=Importance.HIGH, consensus="180K")]

    manual = SimulatedCalendarProvider([_event(title="Non-Farm  Employment Change!", actual="250K")])
    [event] = _pipeline(providers=[Live(), manual]).events()
    assert event.source == "Forex Factory" and event.consensus == "180K" and event.actual == "250K"


def test_a_failing_source_does_not_hide_the_others():
    broken = SimulatedCalendarProvider([_event()])
    broken.fail = True
    pipeline = _pipeline(providers=[broken, SimulatedCalendarProvider([_event(title="CPI m/m")])])
    assert [e.title for e in pipeline.events()] == ["CPI m/m"] and pipeline.available
    assert list(pipeline.errors) == ["1. simulé"]


# ----------------------------------------------------------------- fenêtre de sécurité

@pytest.mark.parametrize("offset_minutes, blocked", [
    (-31, False), (-30, True), (-1, True), (0, True), (15, True), (16, False),
])
def test_window_boundaries(offset_minutes, blocked):
    pipeline = _pipeline(_event())
    block = pipeline.safety_check("EURUSD", ANNOUNCE + timedelta(minutes=offset_minutes))
    assert (block is not None) == blocked
    if block:
        assert block.until == ANNOUNCE + timedelta(minutes=15) and "13:45 UTC" in block.reason


@pytest.mark.parametrize("symbol, event, blocked", [
    ("XAUUSD", _event(), True),                                   # l'or réagit au dollar
    ("GBPJPY", _event(), False),                                  # aucune devise concernée
    ("EURUSD", _event(currency="EUR", title="CPI y/y"), True),
    ("XAUUSD", _event(currency="EUR", title="CPI y/y"), False),
    ("EURUSD", _event(importance="medium"), False),              # seules les annonces fortes bloquent
])
def test_window_only_for_relevant_major_announcements(symbol, event, blocked):
    assert (_pipeline(event).safety_check(symbol, ANNOUNCE) is not None) == blocked


def test_unavailable_calendar_blocks_by_precaution():
    provider = SimulatedCalendarProvider([_event()])
    provider.fail = True
    block = _pipeline(providers=[provider]).safety_check("EURUSD", ANNOUNCE - timedelta(hours=5))
    assert block is not None and "indisponible" in block.reason


def test_alerts_are_sent_once_per_announcement():
    pipeline = _pipeline(_event(), _event(title="Unemployment Rate", minutes=0))
    block = pipeline.safety_check("EURUSD", ANNOUNCE)
    assert pipeline.first_alert(block) is True
    assert pipeline.first_alert(pipeline.safety_check("EURUSD", ANNOUNCE + timedelta(minutes=5))) is False
    assert pipeline.first_alert(pipeline.safety_check("XAUUSD", ANNOUNCE)) is False       # mêmes annonces
    later = _pipeline(_event(title="ISM Services PMI", minutes=120))
    later._alerted = pipeline._alerted
    assert later.first_alert(later.safety_check("EURUSD", ANNOUNCE + timedelta(minutes=120))) is True


def test_engine_blocks_proposals_and_alerts_once(monkeypatch):
    npl.set_pipeline(_pipeline(_event(minutes=10)))               # annonce dans 10 minutes (pour l'heure actuelle)
    pipeline = npl.get_pipeline()
    now = datetime.now(timezone.utc)
    pipeline.providers = [SimulatedCalendarProvider([{**_event(), "time": (now + timedelta(minutes=10)).isoformat()}])]
    alerts, evaluated = [], []
    monkeypatch.setattr(eng, "notify", lambda title, message="", level=None, dedup_key=None: alerts.append(message))
    monkeypatch.setattr(eng, "evaluate_market", lambda symbol, broker: evaluated.append(symbol) or {"action": "HOLD"})
    broker = FakeBroker()
    engine = TradingEngine(symbols=["EURUSD", "GBPJPY"], broker=broker)
    first = engine.step()
    second = engine.step()
    assert [o.status for o in first] == ["news_blocked", "hold"] and second[0].status == "news_blocked"
    assert "Fenêtre de sécurité : annonce majeure Non-Farm" in first[0].verdict
    assert evaluated == ["GBPJPY", "GBPJPY"] and broker.orders == []
    assert sum("Non-Farm" in a for a in alerts) == 1                                # une seule alerte


class _ReadyStrategy(BaseStrategy):
    name = "test"

    def plan_buy(self, data, quote):
        return BuyPlan(SetupStatus.READY, 1.1000, 1.0980, 1.1070, 3.4, MarketRegime.TREND_UP, "test")


def test_setups_are_not_proposed_during_the_window():
    now = datetime.now(timezone.utc)
    npl.set_pipeline(_pipeline({**_event(), "time": (now + timedelta(minutes=20)).isoformat()}))
    setup = _ReadyStrategy().analyze(FakeBroker(), "XAUUSD", now)
    assert setup.status == SetupStatus.INVALIDATED and "fenêtre de sécurité" in setup.notes[-1]
    npl.set_pipeline(_pipeline({**_event(), "time": (now + timedelta(hours=3)).isoformat()}))
    later = _ReadyStrategy().analyze(FakeBroker(), "XAUUSD", now)
    assert not any("fenêtre de sécurité" in n for n in later.notes)


# ----------------------------------------------------------------- analyse structurée

@pytest.mark.parametrize("text, expected", [
    ("250K", 250_000), ("3.2%", 3.2), ("-0.1%", -0.1), ("1,5M", 1_500_000), ("−0.4%", -0.4), ("", None), ("n/a", None),
])
def test_parse_value(text, expected):
    assert parse_value(text) == (pytest.approx(expected) if expected is not None else None)


def _analysed(**values):
    [event] = _pipeline(_event(**values)).events()
    return event


def test_positive_surprise_strengthens_the_currency():
    analysis = analyze_event(_analysed(actual="250K", consensus="180K", previous="150K"))
    assert analysis.surprise.direction == "au-dessus" and analysis.surprise.relative_pct == pytest.approx(38.89, abs=0.01)
    assert analysis.effects["EURUSD"].direction == "baissier" and analysis.effects["XAUUSD"].direction == "baissier"
    assert analysis.effects["EURUSD"].strength == 1.0 and analysis.reliability.value == "élevée"
    assert "250K" in analysis.fact and analysis.horizon.value == "heures"


def test_inverse_indicator_and_base_currency():
    jobless = analyze_event(_analysed(title="Unemployment Rate", actual="4.3%", consensus="4.1%"))
    assert jobless.effects["EURUSD"].direction == "haussier" and any("inversé" in n for n in jobless.notes)
    [eur] = _pipeline(_event(currency="EUR", title="CPI y/y", actual="2.6%", consensus="2.4%")).events()
    assert analyze_event(eur).effects["EURUSD"].direction == "haussier"


def test_unpublished_result_has_no_effect():
    analysis = analyze_event(_analysed(consensus="180K"))
    assert analysis.surprise is None and analysis.effects == {} and "pas encore publié" in analysis.notes[0]


def test_rate_decision_has_a_long_horizon():
    assert analyze_event(_analysed(title="Federal Funds Rate Decision", actual="4.50%", consensus="4.25%")).horizon.value == "jours"


def test_consequence_on_open_setups():
    def setup(side):
        stop, target = (1.0980, 1.1070) if side == Side.BUY else (1.1020, 1.0930)
        return TradingSetup(symbol="EURUSD", side=side, style=TradingStyle.DAY, strategy="test", entry_price=1.1,
                            stop_loss=stop, take_profit=target)

    buy, sell = setup(Side.BUY), setup(Side.SELL)
    analysis = analyze_event(_analysed(actual="250K", consensus="180K"), setups=[buy, sell])
    verdicts = {impact.side: impact.verdict for impact in analysis.setup_impacts}
    assert verdicts == {Side.BUY: "invalidé", Side.SELL: "validé"}
    neutral = analyze_event(_analysed(consensus="180K"), setups=[buy])
    assert neutral.setup_impacts[0].verdict == "neutre"


def test_article_analysis_and_reliability():
    article = SimpleNamespace(title="Euro surges as ECB signals more rate hikes", snippet="", source="Reuters",
                              published=ANNOUNCE)
    analysis = analyze_article(article, symbols=["EURUSD", "XAUUSD"])
    assert analysis.kind == "article" and analysis.reliability.value == "élevée"
    assert analysis.effects["EURUSD"].direction == "haussier"
    assert analyze_article(SimpleNamespace(title="x", snippet="", source="blog inconnu", published=None),
                           symbols=["EURUSD"]).reliability.value == "faible"


def test_duplicate_press_releases_are_kept_once():
    articles = [SimpleNamespace(title="Fed holds rates steady - Reuters", source="Google News", published=ANNOUNCE),
                SimpleNamespace(title="Fed holds rates steady", source="Reuters", published=ANNOUNCE - timedelta(minutes=3)),
                SimpleNamespace(title="ECB cuts rates", source="FXStreet", published=ANNOUNCE)]
    kept = dedupe_articles(articles)
    assert [a.source for a in kept] == ["Reuters", "FXStreet"]                       # le premier publié est gardé


# ----------------------------------------------------------------- fiche d'événement : intensité, validité, action

def test_event_card_before_publication_says_wait():
    card = analyze_event(_analysed(consensus="180K"), now=ANNOUNCE - timedelta(minutes=10))
    assert card.intensity.value == "forte" and card.action.value == "attendre"
    assert card.confidence == pytest.approx(0.45)                     # source fiable, résultat inconnu
    assert card.received_at is not None and card.valid_until == ANNOUNCE + timedelta(hours=4)


def test_strong_surprise_blocks_then_expires():
    event = _analysed(actual="250K", consensus="180K")
    fresh = analyze_event(event, now=ANNOUNCE + timedelta(minutes=5))
    assert fresh.intensity.value == "forte" and fresh.action.value == "bloquer" and fresh.confidence == 0.9
    later = analyze_event(event, now=ANNOUNCE + timedelta(hours=1))
    assert later.action.value == "autoriser"
    expired = analyze_event(event, now=ANNOUNCE + timedelta(hours=5))
    assert expired.action.value == "autoriser" and "périmé" in expired.action_reason


def test_contrary_setup_asks_for_reassessment():
    buy = TradingSetup(symbol="EURUSD", side=Side.BUY, style=TradingStyle.DAY, strategy="test", entry_price=1.1,
                       stop_loss=1.098, take_profit=1.107)
    card = analyze_event(_analysed(actual="250K", consensus="180K"), [buy], now=ANNOUNCE + timedelta(minutes=30))
    assert card.action.value == "réévaluer" and "contre-sens" in card.action_reason


def test_article_card_is_less_confident_than_a_published_figure():
    article = SimpleNamespace(title="Euro surges as ECB signals more rate hikes", snippet="", source="Reuters",
                              published=ANNOUNCE, received_at=ANNOUNCE + timedelta(seconds=40))
    card = analyze_article(article, symbols=["EURUSD"], now=ANNOUNCE + timedelta(minutes=1))
    assert card.confidence == pytest.approx(0.54) and card.received_at == ANNOUNCE + timedelta(seconds=40)


def test_pipeline_cards_record_reception_time():
    pipeline = _pipeline(_event(actual="250K", consensus="180K"), _event(title="Retail Sales", importance="low"))
    [card] = pipeline.cards(now=ANNOUNCE + timedelta(minutes=5))      # importance faible exclue
    assert card.received_at is not None and card.action.value == "bloquer"
