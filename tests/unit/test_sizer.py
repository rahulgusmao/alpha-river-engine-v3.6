"""tests/unit/test_sizer.py — Unit tests para Sizer."""

import pytest
from src.risk.sizer import Sizer


@pytest.fixture
def sizer():
    return Sizer(risk_per_trade_pct=0.01, delev_multiplier=0.5, min_notional_usdt=5.0)


class TestSizer:
    def test_normal_long_build(self, sizer):
        qty, notional = sizer.compute(10_000.0, 100.0, "LONG_BUILD")
        assert notional == pytest.approx(100.0)  # 1% de 10k
        assert qty == pytest.approx(1.0)          # 100 USDT / 100

    def test_delev_regime_halves_position(self, sizer):
        qty_normal, _ = sizer.compute(10_000.0, 100.0, "NEUTRO")
        qty_delev, _  = sizer.compute(10_000.0, 100.0, "DELEV")
        assert qty_delev == pytest.approx(qty_normal * 0.5)

    def test_below_min_notional_returns_zero(self, sizer):
        # 0.01% de 10 USDT = 0.001 USDT < 5 USDT mínimo
        qty, notional = sizer.compute(10.0, 100.0, "NEUTRO")
        assert qty == 0.0
        assert notional == 0.0

    def test_zero_portfolio_returns_zero(self, sizer):
        qty, notional = sizer.compute(0.0, 100.0, "NEUTRO")
        assert qty == 0.0
        assert notional == 0.0

    def test_high_price_reduces_qty(self, sizer):
        qty_low,  _ = sizer.compute(10_000.0, 1.0,    "NEUTRO")
        qty_high, _ = sizer.compute(10_000.0, 10_000.0, "NEUTRO")
        assert qty_low > qty_high

    def test_all_oi_regimes_accepted(self, sizer):
        for regime in ("LONG_BUILD", "NEUTRO", "SHORT_BUILD", "DELEV"):
            qty, _ = sizer.compute(10_000.0, 100.0, regime)
            assert qty >= 0.0
