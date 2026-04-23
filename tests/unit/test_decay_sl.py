"""tests/unit/test_decay_sl.py — Unit tests para DecaySLChecker."""

import pytest
from src.risk.decay_sl import DecaySLChecker, DecaySLConfig


@pytest.fixture
def cfg():
    return DecaySLConfig(
        phase1_candles=4,
        phase2_candles=16,
        mult_phase1=2.0,
        mult_phase2=1.7,
        mult_phase3=0.8,
    )


@pytest.fixture
def checker(cfg):
    return DecaySLChecker(cfg)


class TestDecaySLMultiplier:
    def test_phase1_at_candle_0(self, checker):
        sl = checker.compute_sl(100.0, 5.0, candles_open=0)
        assert sl == pytest.approx(100.0 - 2.0 * 5.0)

    def test_phase1_at_candle_4(self, checker):
        sl = checker.compute_sl(100.0, 5.0, candles_open=4)
        assert sl == pytest.approx(90.0)

    def test_phase2_at_candle_5(self, checker):
        sl = checker.compute_sl(100.0, 5.0, candles_open=5)
        assert sl == pytest.approx(100.0 - 1.7 * 5.0)

    def test_phase2_at_candle_16(self, checker):
        sl = checker.compute_sl(100.0, 5.0, candles_open=16)
        assert sl == pytest.approx(100.0 - 1.7 * 5.0)

    def test_phase3_at_candle_17(self, checker):
        sl = checker.compute_sl(100.0, 5.0, candles_open=17)
        assert sl == pytest.approx(100.0 - 0.8 * 5.0)

    def test_zero_atr_returns_zero(self, checker):
        sl = checker.compute_sl(100.0, 0.0, candles_open=5)
        assert sl == 0.0


class TestDecaySLCheck:
    def test_not_triggered_when_close_above_sl(self, checker):
        sl, triggered = checker.check(100.0, 5.0, candles_open=5, current_close=92.0, current_sl_price=80.0)
        assert not triggered

    def test_triggered_when_close_below_decay_sl(self, checker):
        # Phase 3 (c=17): sl = 100 - 0.8*5 = 96. Close=95 → triggered
        sl, triggered = checker.check(100.0, 5.0, candles_open=17, current_close=95.0, current_sl_price=80.0)
        assert triggered
        assert sl == pytest.approx(96.0)

    def test_decay_sl_never_lowers_existing_sl(self, checker):
        # current_sl=95 é mais alto que decay_sl=90 → effective_sl=95
        sl, triggered = checker.check(100.0, 5.0, candles_open=0, current_close=94.0, current_sl_price=95.0)
        assert sl == pytest.approx(95.0)
        assert triggered  # 94 < 95

    def test_zero_atr_never_triggers(self, checker):
        sl, triggered = checker.check(100.0, 0.0, candles_open=20, current_close=50.0, current_sl_price=80.0)
        assert not triggered


class TestDecaySLFromConfig:
    def test_from_config_defaults(self):
        cfg = DecaySLConfig.from_config({})
        assert cfg.phase1_candles == 4
        assert cfg.mult_phase1 == 2.0
        assert cfg.mult_phase3 == 0.8

    def test_from_config_custom(self):
        cfg = DecaySLConfig.from_config({
            "sl_decay_phase1_candles": 5,
            "sl_decay_mult_phase3": 0.5,
        })
        assert cfg.phase1_candles == 5
        assert cfg.mult_phase3 == 0.5
