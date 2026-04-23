"""tests/unit/test_breakeven.py — Unit tests para BreakevenStop + MFE Gate."""

import pytest
from src.risk.breakeven import BreakevenConfig, BreakevenStop


@pytest.fixture
def cfg():
    return BreakevenConfig(
        trigger_candles=4,
        loss_cap_pct=0.50,
        mfe_exit_threshold_pct=0.05,
        mfe_exit_cvd_threshold=-0.50,
        mfe_runoff_threshold_pct=0.50,
        mfe_runoff_cvd_min=0.0,
    )


@pytest.fixture
def be(cfg):
    return BreakevenStop(cfg)


class TestBreakevenCheck:
    def test_before_trigger_no_action(self, be):
        sl, triggered = be.check(100.0, 97.0, candles_open=3, trailing_active=False, current_close=99.0)
        assert sl == 97.0
        assert not triggered

    def test_trailing_active_bypasses_be(self, be):
        sl, triggered = be.check(100.0, 98.0, candles_open=6, trailing_active=True, current_close=95.0)
        assert sl == 98.0
        assert not triggered

    def test_at_trigger_candle_moves_sl_to_entry(self, be):
        sl, triggered = be.check(100.0, 97.0, candles_open=4, trailing_active=False, current_close=101.0)
        assert sl == pytest.approx(100.0)
        assert not triggered  # acima do entry → não fecha

    def test_cap_loss_triggers_close(self, be):
        # close <= entry × (1 - 0.50%) → fecha
        entry = 100.0
        close = entry * (1.0 - 0.005) - 0.001  # levemente abaixo do cap
        sl, triggered = be.check(entry, 97.0, candles_open=4, trailing_active=False, current_close=close)
        assert triggered

    def test_sl_at_entry_triggers_close(self, be):
        # close == entry → SL no entry tocado → fecha
        sl, triggered = be.check(100.0, 97.0, candles_open=5, trailing_active=False, current_close=100.0)
        assert triggered

    def test_below_entry_above_cap_triggers_close(self, be):
        # entry=100, cap_floor=99.5, close=99.9 → close > cap_floor, close < new_sl=100 → triggered
        sl, triggered = be.check(100.0, 97.0, candles_open=5, trailing_active=False, current_close=99.9)
        assert triggered

    def test_above_entry_keeps_trade_open(self, be):
        sl, triggered = be.check(100.0, 97.0, candles_open=5, trailing_active=False, current_close=102.0)
        assert not triggered
        assert sl == pytest.approx(100.0)


class TestMFEExitGate:
    def test_triggers_on_c3_low_mfe_bad_cvd(self, be):
        # candles=3, MFE=0%, cvd=-0.8 → sair
        assert be.check_mfe_exit(100.0, peak_price=100.0, current_close=99.0, cvd_n=-0.8, candles_open=3)

    def test_no_trigger_on_c3_with_positive_mfe(self, be):
        # MFE > 0.05% → não sair
        assert not be.check_mfe_exit(100.0, peak_price=100.1, current_close=100.0, cvd_n=-0.9, candles_open=3)

    def test_no_trigger_on_c3_with_positive_cvd(self, be):
        # cvd > threshold → não sair
        assert not be.check_mfe_exit(100.0, peak_price=100.0, current_close=99.0, cvd_n=0.5, candles_open=3)

    def test_no_trigger_before_c3(self, be):
        assert not be.check_mfe_exit(100.0, peak_price=100.0, current_close=99.0, cvd_n=-0.9, candles_open=2)

    def test_no_trigger_after_c3(self, be):
        assert not be.check_mfe_exit(100.0, peak_price=100.0, current_close=99.0, cvd_n=-0.9, candles_open=4)


class TestMFERunoffGate:
    def test_allows_runoff_with_high_mfe_positive_cvd(self, be):
        # MFE=1%, cvd=0.3, c4 → runoff
        assert be.check_mfe_runoff(100.0, peak_price=101.0, cvd_n=0.3, candles_open=4, trailing_active=False)

    def test_no_runoff_if_trailing_active(self, be):
        assert not be.check_mfe_runoff(100.0, peak_price=101.0, cvd_n=0.3, candles_open=4, trailing_active=True)

    def test_no_runoff_before_trigger_candle(self, be):
        assert not be.check_mfe_runoff(100.0, peak_price=101.0, cvd_n=0.3, candles_open=3, trailing_active=False)

    def test_no_runoff_with_low_mfe(self, be):
        # MFE < 0.50% → não runoff
        assert not be.check_mfe_runoff(100.0, peak_price=100.3, cvd_n=0.3, candles_open=4, trailing_active=False)

    def test_no_runoff_with_negative_cvd(self, be):
        # cvd < 0 → não runoff
        assert not be.check_mfe_runoff(100.0, peak_price=101.0, cvd_n=-0.1, candles_open=4, trailing_active=False)
