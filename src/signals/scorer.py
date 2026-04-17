"""
src/signals/scorer.py
Cálculo de score por tier — configuração canônica v3.2.

Fórmulas:
─────────────────────────────────────────────────────────────
Tier 1 e 2:
    Score = (0.40×zvol_n + 0.40×cvd_n + 0.10×lsr_n + 0.10×adx_n) × oi_mult

Tier 3 — Kalman ATIVO (kal_active=True, R² < 0.30):
    Score = (0.34×zvol_n + 0.22×cvd_n + 0.07×lsr_n + 0.07×adx_n + 0.30×kal) × oi_mult

Tier 3 — Kalman INATIVO (kal_active=False, R² ≥ 0.30):
    Os 0.30 de peso do Kalman são redistribuídos proporcionalmente aos demais:
    factor = 1 / (1 - 0.30) = 1 / 0.70 ≈ 1.4286
    zvol: 0.34 × factor ≈ 0.4857
    cvd:  0.22 × factor ≈ 0.3143
    lsr:  0.07 × factor = 0.10
    adx:  0.07 × factor = 0.10
    Score = (0.4857×zvol_n + 0.3143×cvd_n + 0.10×lsr_n + 0.10×adx_n) × oi_mult
─────────────────────────────────────────────────────────────

Nota sobre Kalman em Tier3 com kal=None:
    Se kal for None (janela de inovações insuficiente durante warm-up),
    o scorer trata como Kalman inativo e redistribui os pesos.
    Isso garante que sinais Tier3 ainda possam ser emitidos antes de
    completar a janela mínima do Kalman (20 candles de inovação).
"""

from src.features.models import FeatureSet

# ── Pesos Tier 1/2 ────────────────────────────────────────────────────────
_W12_ZVOL = 0.40
_W12_CVD  = 0.40
_W12_LSR  = 0.10
_W12_ADX  = 0.10

# ── Pesos Tier 3 com Kalman ativo ─────────────────────────────────────────
_W3_ZVOL = 0.34
_W3_CVD  = 0.22
_W3_LSR  = 0.07
_W3_ADX  = 0.07
_W3_KAL  = 0.30

# ── Pesos Tier 3 com Kalman inativo (redistribuídos) ──────────────────────
# factor = 1 / (1 - _W3_KAL) = 1 / 0.70
_W3_NO_KAL_ZVOL = _W3_ZVOL / (1.0 - _W3_KAL)   # ≈ 0.4857
_W3_NO_KAL_CVD  = _W3_CVD  / (1.0 - _W3_KAL)   # ≈ 0.3143
_W3_NO_KAL_LSR  = _W3_LSR  / (1.0 - _W3_KAL)   # = 0.10
_W3_NO_KAL_ADX  = _W3_ADX  / (1.0 - _W3_KAL)   # = 0.10


class Scorer:
    """
    Computa o score para um FeatureSet de acordo com o tier e estado do Kalman.
    Stateless — não mantém estado por símbolo.
    """

    def compute(self, fs: FeatureSet) -> float:
        """
        Calcula o score e atualiza fs.score in-place.

        Args:
            fs: FeatureSet com todas as features normalizadas preenchidas.

        Returns:
            score (float) — também armazenado em fs.score.
        """
        if fs.tier in (1, 2):
            raw = self._score_tier12(fs)
        else:
            raw = self._score_tier3(fs)

        score = raw * fs.oi_mult
        fs.score = float(score)
        return fs.score

    # ──────────────────────────── TIER 1/2 ────────────────────────────────

    def _score_tier12(self, fs: FeatureSet) -> float:
        """
        Tier 1 e 2: fórmula sem Kalman.
        Score = (0.40×zvol_n + 0.40×cvd_n + 0.10×lsr_n + 0.10×adx_n)
        """
        return (
            _W12_ZVOL * fs.zvol_n
            + _W12_CVD * fs.cvd_n
            + _W12_LSR * fs.lsr_n
            + _W12_ADX * fs.adx_n
        )

    # ──────────────────────────── TIER 3 ──────────────────────────────────

    def _score_tier3(self, fs: FeatureSet) -> float:
        """
        Tier 3: usa Kalman se ativo e disponível, caso contrário redistribui pesos.
        """
        kal_usable = fs.kal_active and fs.kal is not None

        if kal_usable:
            return (
                _W3_ZVOL * fs.zvol_n
                + _W3_CVD  * fs.cvd_n
                + _W3_LSR  * fs.lsr_n
                + _W3_ADX  * fs.adx_n
                + _W3_KAL  * fs.kal
            )
        else:
            # Kalman inativo ou ainda sem dados → redistribui os 0.30 de peso
            return (
                _W3_NO_KAL_ZVOL * fs.zvol_n
                + _W3_NO_KAL_CVD * fs.cvd_n
                + _W3_NO_KAL_LSR * fs.lsr_n
                + _W3_NO_KAL_ADX * fs.adx_n
            )
