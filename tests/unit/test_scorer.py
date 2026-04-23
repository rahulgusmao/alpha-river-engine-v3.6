"""tests/unit/test_scorer.py — Unit tests para Scorer."""

import pytest
from src.signals.scorer import Scorer
from src.features.models import FeatureSet, OIRegime


def make_fs(**kwargs) -> FeatureSet:
    defaults = dict(
        symbol="TESTUSDT",
        ts_close=1_000_000,
        tier=1,
        zvol_n=0.5,
        zvol_raw=1.5,
        cvd_n=0.5,
        lsr_n=0.5,
        adx_n=0.5,
        kal=None,
        kal_active=False,
        kal_r2=None,
        atr14=10.0,
        rsi=50.0,
        close=100.0,
        oi_regime="NEUTRO",
        oi_mult=1.0,
    )
    defaults.update(kwargs)
    return FeatureSet(**defaults)


@pytest.fixture
def scorer():
    return Scorer()


class TestScorerTier12:
    def test_neutral_features_give_near_zero_score(self, scorer):
        fs = make_fs(zvol_n=0.0, cvd_n=0.0, lsr_n=0.0, adx_n=0.0)
        score = scorer.compute(fs)
        assert score == pytest.approx(0.0)

    def test_all_positive_features_give_positive_score(self, scorer):
        fs = make_fs(zvol_n=1.0, cvd_n=1.0, lsr_n=1.0, adx_n=1.0, oi_mult=1.0)
        score = scorer.compute(fs)
        assert score == pytest.approx(1.0)

    def test_oi_mult_scales_score(self, scorer):
        fs_base = make_fs(zvol_n=0.5, cvd_n=0.5, lsr_n=0.5, adx_n=0.5, oi_mult=1.0)
        fs_long = make_fs(zvol_n=0.5, cvd_n=0.5, lsr_n=0.5, adx_n=0.5, oi_mult=1.5)
        assert scorer.compute(fs_long) == pytest.approx(scorer.compute(fs_base) * 1.5)

    def test_score_written_to_fs(self, scorer):
        fs = make_fs(zvol_n=0.5, cvd_n=0.5, lsr_n=0.5, adx_n=0.5)
        score = scorer.compute(fs)
        assert fs.score == score

    def test_tier2_uses_same_formula_as_tier1(self, scorer):
        fs1 = make_fs(tier=1, zvol_n=0.3, cvd_n=0.4, lsr_n=0.2, adx_n=0.1, oi_mult=1.0)
        fs2 = make_fs(tier=2, zvol_n=0.3, cvd_n=0.4, lsr_n=0.2, adx_n=0.1, oi_mult=1.0)
        assert scorer.compute(fs1) == pytest.approx(scorer.compute(fs2))


class TestScorerTier3:
    def test_without_kalman_uses_base_weights(self, scorer):
        fs = make_fs(tier=3, zvol_n=1.0, cvd_n=0.0, lsr_n=0.0, adx_n=0.0,
                     kal=None, kal_r2=None, oi_mult=1.0)
        score = scorer.compute(fs)
        assert score == pytest.approx(0.4857, abs=1e-3)

    def test_high_r2_ignores_kalman(self, scorer):
        # R² > 0.35 → kal_weight = 0, score = base
        fs_no_kal = make_fs(tier=3, zvol_n=0.5, cvd_n=0.5, lsr_n=0.0, adx_n=0.0,
                            kal=0.9, kal_r2=0.40, oi_mult=1.0)
        fs_base   = make_fs(tier=3, zvol_n=0.5, cvd_n=0.5, lsr_n=0.0, adx_n=0.0,
                            kal=None, kal_r2=None, oi_mult=1.0)
        assert scorer.compute(fs_no_kal) == pytest.approx(scorer.compute(fs_base), abs=1e-6)

    def test_low_r2_uses_max_kalman_weight(self, scorer):
        # R² < 0.25 → kal_weight = 0.20
        fs = make_fs(tier=3, zvol_n=0.0, cvd_n=0.0, lsr_n=0.0, adx_n=0.0,
                     kal=1.0, kal_r2=0.10, oi_mult=1.0)
        score = scorer.compute(fs)
        assert score == pytest.approx(0.20, abs=1e-6)

    def test_score_finite(self, scorer):
        import math
        for r2 in [0.0, 0.10, 0.25, 0.30, 0.35, 0.50]:
            fs = make_fs(tier=3, zvol_n=0.5, cvd_n=0.3, lsr_n=0.1, adx_n=0.1,
                         kal=0.5, kal_r2=r2, oi_mult=1.0)
            score = scorer.compute(fs)
            assert math.isfinite(score), f"Score não-finito para r2={r2}: {score}"
