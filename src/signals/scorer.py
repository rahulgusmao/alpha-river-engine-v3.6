"""
src/signals/scorer.py
Cálculo de score por tier — configuração canônica v4.0.

Revisão Arquitetural P73 / P33 / P34 (ANALYTICAL_FRAMEWORK Parte XIII, seção 13.3):
─────────────────────────────────────────────────────────────
Kalman DESATIVADO para Tier 1 e Tier 2:
    R² médio Tier1 = 0.488, Tier2 ≈ 0.35 → Alpha residual é ruído estrutural.
    WR Tipo2 N=5 nos 10 majors = 48.5% ≈ aleatório. Gate removido permanentemente.

Kalman MANTIDO apenas para Tier 3, com peso DINÂMICO (P34):
    Critério de elegibilidade: R²(token, BTC) — calculado em KalmanCalculator.
      R² < 0.25  → kal_weight = KAL_MAX (0.20) — Alpha idiossincrático, full weight
      R² > 0.35  → kal_weight = 0.0             — sem edge, Kalman ignorado
      0.25 ≤ R² ≤ 0.35 → kal_weight = KAL_MAX × (0.35 − R²) / 0.10  (proporcional)
    O restante (1 − kal_weight) é distribuído para zvol/cvd/lsr/adx
    em proporção fixa (0.4857 / 0.3143 / 0.10 / 0.10).

Fórmulas resultantes:
─────────────────────────────────────────────────────────────
Tier 1 (majors, R² 0.40–0.65 c/ BTC):
    Score = (0.40×zvol_n + 0.40×cvd_n + 0.10×lsr_n + 0.10×adx_n) × oi_mult
    Kalman: IGNORADO.

Tier 2 ($50M–$500M/dia, R² 0.25–0.45 c/ BTC):
    Score = (0.40×zvol_n + 0.40×cvd_n + 0.10×lsr_n + 0.10×adx_n) × oi_mult
    Kalman: REMOVIDO (AF 13.3: opcional; instrução v4.0: desativado por padrão).

Tier 3 ($5M–$50M/dia, R² 0.05–0.30 c/ BTC) — peso dinâmico:
    w_kal   = clamp(KAL_MAX × (0.35 − r2) / 0.10, 0.0, KAL_MAX)
    w_rest  = 1.0 − w_kal
    Score = (0.4857×w_rest×zvol_n + 0.3143×w_rest×cvd_n
             + 0.10×w_rest×lsr_n  + 0.10×w_rest×adx_n
             + w_kal×kal) × oi_mult

    Exemplos de w_kal por R²:
      R²=0.10 → w_kal=0.200, w_rest=0.800 → zvol=0.389, cvd=0.251, kal=0.200
      R²=0.25 → w_kal=0.200, w_rest=0.800 → (limiar inferior, peso máximo)
      R²=0.30 → w_kal=0.100, w_rest=0.900 → zvol=0.437, cvd=0.283, kal=0.100
      R²=0.35 → w_kal=0.000, w_rest=1.000 → zvol=0.486, cvd=0.314, kal=0.000
─────────────────────────────────────────────────────────────

Nota: se kal for None (warm-up insuficiente), w_kal colapsa para 0
e o score continua válido com redistribuição total para zvol/cvd/lsr/adx.
"""

from src.features.models import FeatureSet

# ── Pesos Tier 1/2 (Kalman removido — P73 / AF 13.3) ─────────────────────
_W12_ZVOL = 0.40
_W12_CVD  = 0.40
_W12_LSR  = 0.10
_W12_ADX  = 0.10

# ── Proporções base Tier 3 sem Kalman (soma = 1.0) ────────────────────────
# Referência: pesos originais Tier3 / (1 - 0.30) = normalização sem Kalman
_W3_BASE_ZVOL = 0.4857   # = 0.34 / 0.70
_W3_BASE_CVD  = 0.3143   # = 0.22 / 0.70
_W3_BASE_LSR  = 0.1000   # = 0.07 / 0.70
_W3_BASE_ADX  = 0.1000   # = 0.07 / 0.70

# ── Parâmetros P34: elegibilidade Kalman em Tier 3 (AF 13.3) ─────────────
_KAL_R2_FULL_BELOW  = 0.25   # R² abaixo → peso máximo (Alpha genuíno)
_KAL_R2_ZERO_ABOVE  = 0.35   # R² acima  → peso zero (Kalman sem edge)
_KAL_MAX_WEIGHT     = 0.20   # peso máximo Kalman no score (AF 13.3: ≤ 0.20)


class Scorer:
    """
    Computa o score para um FeatureSet de acordo com o tier.

    v4.0: Kalman removido de Tier1/2 (P73). Tier3 usa peso dinâmico via R² (P34).
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

    # ──────────────────────────── TIER 1 / 2 ──────────────────────────────

    def _score_tier12(self, fs: FeatureSet) -> float:
        """
        Tier 1 e 2: sem Kalman (P73 / AF 13.3).
        R² alto com BTC → Alpha residual é ruído. Gate removido permanentemente.
        Score = 0.40×zvol_n + 0.40×cvd_n + 0.10×lsr_n + 0.10×adx_n
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
        Tier 3: peso Kalman dinâmico via R² (P34 — AF 13.3).

        w_kal cresce conforme R² cai (menos correlação com BTC = mais Alpha genuíno).
        Se kal for None (warm-up insuficiente), w_kal = 0 e score continua válido.
        """
        w_kal = self._kalman_weight(fs)

        # Kalman sem peso ou feature não disponível: score base sem Kalman
        if w_kal <= 0.0 or fs.kal is None:
            return (
                _W3_BASE_ZVOL * fs.zvol_n
                + _W3_BASE_CVD * fs.cvd_n
                + _W3_BASE_LSR * fs.lsr_n
                + _W3_BASE_ADX * fs.adx_n
            )

        # Distribuição dinâmica: (1 − w_kal) para os 4 indicadores base
        w_rest = 1.0 - w_kal
        return (
            _W3_BASE_ZVOL * w_rest * fs.zvol_n
            + _W3_BASE_CVD * w_rest * fs.cvd_n
            + _W3_BASE_LSR * w_rest * fs.lsr_n
            + _W3_BASE_ADX * w_rest * fs.adx_n
            + w_kal        * fs.kal
        )

    # ──────────────────────────── HELPERS ─────────────────────────────────

    def _kalman_weight(self, fs: FeatureSet) -> float:
        """
        Calcula o peso Kalman via critério P34 (AF 13.3).

        Retorna valor em [0.0, _KAL_MAX_WEIGHT] baseado no R² rolling
        do token com BTC. Retorna 0.0 se R² não disponível (warm-up).
        """
        r2 = fs.kal_r2
        if r2 is None:
            return 0.0

        if r2 >= _KAL_R2_ZERO_ABOVE:
            return 0.0                           # R² alto → sem Alpha idiossincrático

        if r2 <= _KAL_R2_FULL_BELOW:
            return _KAL_MAX_WEIGHT               # R² baixo → Alpha genuíno, peso máximo

        # Faixa intermediária: interpolação linear decrescente com o R²
        frac = (_KAL_R2_ZERO_ABOVE - r2) / (_KAL_R2_ZERO_ABOVE - _KAL_R2_FULL_BELOW)
        return round(_KAL_MAX_WEIGHT * frac, 6)
