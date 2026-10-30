"""Profil de stratégie « scalping » : valeurs du profil, priorité du .env, indicateurs rapides, SL ≥ n spreads."""

import importlib
from dataclasses import replace

import pandas as pd
import pytest

from strategies import strategy_manager as sm
from strategies import technical_analysis as ta
from strategies.technical_analysis import INDICATOR_PROFILES, TechnicalSignal

settings_module = importlib.import_module("config.settings")

PROFILE_KEYS = ("TRADING_TIMEFRAME", "BOT_LOOP_INTERVAL_SECONDS", "STRATEGY_TECHNICAL_WEIGHT",
                "STRATEGY_SENTIMENT_WEIGHT", "STRATEGY_SIGNAL_THRESHOLD", "STRATEGY_TECHNICAL_THRESHOLD",
                "STRATEGY_MIN_SL_SPREADS", "STRATEGY_STRUCTURAL_SL", "RISK_SL_ATR_MULTIPLIER", "RISK_TP_ATR_MULTIPLIER")


def test_scalping_profile_defaults(monkeypatch):
    for key in PROFILE_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("STRATEGY_PROFILE", "scalping")
    cfg = settings_module.load_settings()
    assert cfg.trading.timeframe == "M1" and cfg.loop_interval_seconds == 10
    assert (cfg.strategy.signal_threshold, cfg.strategy.technical_threshold) == (0.2, 0.2)
    assert (cfg.strategy.technical_weight, cfg.strategy.sentiment_weight) == (0.85, 0.15)
    assert (cfg.risk.sl_atr_multiplier, cfg.risk.tp_atr_multiplier) == (1.0, 1.2)
    assert cfg.strategy.min_sl_spreads == 3 and cfg.strategy.structural_sl is False


def test_env_values_take_precedence_and_empty_means_profile(monkeypatch):
    monkeypatch.setenv("STRATEGY_PROFILE", "scalping")
    monkeypatch.setenv("TRADING_TIMEFRAME", "M5")
    monkeypatch.setenv("STRATEGY_SIGNAL_THRESHOLD", "")
    cfg = settings_module.load_settings()
    assert cfg.trading.timeframe == "M5" and cfg.strategy.signal_threshold == 0.2


def test_unknown_profile_is_rejected(monkeypatch):
    monkeypatch.setenv("STRATEGY_PROFILE", "yolo")
    with pytest.raises(settings_module.ConfigError, match="STRATEGY_PROFILE"):
        settings_module.load_settings()


def test_scalping_indicators_need_few_bars_and_react_faster():
    closes = [1.10] * 40 + [1.10 + 0.0004 * i for i in range(1, 8)]          # micro-impulsion de 7 bougies
    df = pd.DataFrame({"open": closes, "high": [c + 0.0001 for c in closes],
                       "low": [c - 0.0001 for c in closes], "close": closes})
    fast = ta.signal_from_indicators(ta.compute_indicators(df, INDICATOR_PROFILES["scalping"]),
                                     params=INDICATOR_PROFILES["scalping"])
    slow = ta.signal_from_indicators(ta.compute_indicators(df, INDICATOR_PROFILES["standard"]),
                                     params=INDICATOR_PROFILES["standard"])
    assert fast.is_valid and fast.signal == "BUY" and "EMA5/EMA13" in fast.reasons[0]
    assert not slow.is_valid                                                   # EMA50 pas encore calculable


def _tech(atr=0.0002) -> TechnicalSignal:
    return TechnicalSignal(signal="BUY", score=0.5, close=1.1, atr=atr, swing_low=1.0990, swing_high=1.1010)


def test_sl_is_at_least_n_spreads_and_tp_keeps_ratio(monkeypatch):
    scalping = replace(sm.settings, strategy=replace(sm.settings.strategy, min_sl_spreads=3, structural_sl=False),
                       risk=replace(sm.settings.risk, sl_atr_multiplier=1.0, tp_atr_multiplier=1.2))
    monkeypatch.setattr(sm, "settings", scalping)
    sl, tp = sm.compute_sl_tp("BUY", 1.1, _tech(atr=0.0002), spread=0.0001)   # 3 spreads > 1 ATR
    assert sl == pytest.approx(1.1 - 0.0003) and tp == pytest.approx(1.1 + 0.00036)
    sl, _ = sm.compute_sl_tp("BUY", 1.1, _tech(atr=0.0005), spread=0.0001)    # ATR plus large : ATR
    assert sl == pytest.approx(1.1 - 0.0005)                                    # pas d'élargissement au plus bas


# ----------------------------------------------------------------- profil micro : aucun seuil, SL / TP en pips

def test_micro_profile_defaults(monkeypatch):
    for key in PROFILE_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("STRATEGY_PROFILE", "micro")
    cfg = settings_module.load_settings()
    assert (cfg.strategy.signal_threshold, cfg.strategy.technical_threshold) == (0.0, 0.0)
    assert (cfg.strategy.technical_weight, cfg.strategy.sentiment_weight) == (1.0, 0.0)
    assert (cfg.strategy.sl_pips, cfg.strategy.tp_pips) == (5.0, 5.0) and cfg.trading.timeframe == "M1"


@pytest.mark.parametrize("sl, tp", [("5", "0"), ("5", "3"), ("-1", "-1")])
def test_invalid_pip_settings_are_rejected(monkeypatch, sl, tp):
    monkeypatch.setenv("STRATEGY_PROFILE", "micro")
    monkeypatch.setenv("STRATEGY_SL_PIPS", sl)
    monkeypatch.setenv("STRATEGY_TP_PIPS", tp)
    with pytest.raises(settings_module.ConfigError, match="PIPS"):
        settings_module.load_settings()


def _micro(monkeypatch):
    micro = replace(sm.settings, strategy=replace(sm.settings.strategy, signal_threshold=0.0, technical_threshold=0.0,
                                                  technical_weight=1.0, sentiment_weight=0.0, min_sl_spreads=3,
                                                  structural_sl=False, sl_pips=5, tp_pips=5))
    monkeypatch.setattr(sm, "settings", micro)
    monkeypatch.setattr(ta, "settings", micro)


def test_any_non_zero_technical_score_trades(monkeypatch):
    _micro(monkeypatch)
    weak = TechnicalSignal(signal="BUY", score=0.01, close=1.1, atr=0.0002)
    assert sm.decide(weak, sentiment=-1.0, news_blocked=False).action == "BUY"     # sentiment sans effet
    flat = TechnicalSignal(signal="NEUTRAL", score=0.0, close=1.1, atr=0.0002)
    assert sm.decide(flat, sentiment=1.0, news_blocked=False).action == "HOLD"      # aucun mouvement
    assert sm.decide(weak, sentiment=0.0, news_blocked=True).action == "HOLD"       # calendrier conservé


def test_fixed_pip_levels_and_pip_sizes(monkeypatch, eurusd_specs):
    _micro(monkeypatch)
    pip = sm.pip_size(eurusd_specs)
    assert pip == pytest.approx(0.0001)
    sl, tp = sm.compute_sl_tp("SELL", 1.1, _tech(), spread=0.00001, pip=pip)
    assert sl == pytest.approx(1.1005) and tp == pytest.approx(1.0995)
    sl, _ = sm.compute_sl_tp("BUY", 1.1, _tech(), spread=0.0002, pip=pip)          # 3 spreads = 6 pips > 5
    assert sl == pytest.approx(1.1 - 0.0006)
    gold = replace(eurusd_specs, symbol="XAUUSD", digits=2, point=0.01, tick_size=0.01)
    jpy = replace(eurusd_specs, symbol="USDJPY", digits=3, point=0.001, tick_size=0.001)
    assert (sm.pip_size(gold), sm.pip_size(jpy), sm.pip_size(None)) == (pytest.approx(0.1), pytest.approx(0.01), 0.0)


def test_technical_threshold_zero_keeps_flat_score_neutral(monkeypatch):
    _micro(monkeypatch)
    closes = [1.1 + 0.00002 * ((-1) ** i) for i in range(80)]
    df = pd.DataFrame({"open": closes, "high": [c + 0.0001 for c in closes],
                       "low": [c - 0.0001 for c in closes], "close": closes})
    signal = ta.signal_from_indicators(ta.compute_indicators(df, INDICATOR_PROFILES["scalping"]),
                                       params=INDICATOR_PROFILES["scalping"])
    assert signal.is_valid and signal.signal == ("NEUTRAL" if signal.score == 0 else
                                                 "BUY" if signal.score > 0 else "SELL")


def test_every_profile_has_indicator_periods():
    assert set(INDICATOR_PROFILES) == set(settings_module.STRATEGY_PROFILES)
    assert INDICATOR_PROFILES["micro"].ema_fast == 5
