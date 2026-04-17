"""
src/features/models.py
Modelos de saída do Feature Engine.

FeatureSet é o contrato de interface entre Feature Engine e Signal Engine.
Contém todas as features normalizadas prontas para computação de score.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class OIRegime(str, Enum):
    """
    Regime de Open Interest — classificação do comportamento institucional.

    LONG_BUILD  : OI crescendo + preço subindo → posições compradas abrindo
    SHORT_BUILD : OI crescendo + preço caindo  → posições vendidas abrindo
    DELEV       : OI caindo → desalavancagem geral (risk-off)
    NEUTRO      : variação de OI dentro do threshold → sem sinal institucional claro
    """
    LONG_BUILD  = "LONG_BUILD"
    SHORT_BUILD = "SHORT_BUILD"
    DELEV       = "DELEV"
    NEUTRO      = "NEUTRO"


# Multiplicadores de score por regime OI (configuração canônica v3.2)
OI_MULTIPLIERS: dict[OIRegime, float] = {
    OIRegime.LONG_BUILD:  1.5,
    OIRegime.NEUTRO:      1.0,
    OIRegime.SHORT_BUILD: 0.8,
    OIRegime.DELEV:       0.5,
}


@dataclass
class FeatureSet:
    """
    Conjunto de features normalizadas para um símbolo em um candle fechado.

    Produzido pelo FeatureEngine e consumido pelo SignalEngine.
    Todos os campos _n são normalizados para seus respectivos intervalos.

    Campos None indicam dados insuficientes para computação (ex: símbolo novo,
    histórico menor que a janela mínima). O SignalEngine deve descartar esses casos.
    """

    symbol:   str
    ts_close: int     # epoch ms — timestamp de fechamento do candle
    tier:     int     # 1, 2 ou 3 — determina a fórmula de score

    # ── Features normalizadas para o score ────────────────────────────────

    zvol_n: Optional[float] = None
    """Z-Score de volume normalizado via tanh(z/2) → [-1, 1].
    Alto zvol_n = volume anormalmente alto = possível evento institucional."""

    cvd_n: Optional[float] = None
    """CVD delta normalizado via tanh → [-1, 1].
    Positivo = takers comprando mais do que vendendo = pressão compradora."""

    lsr_n: Optional[float] = None
    """Long/Short Ratio normalizado via min-max rolling → [0, 1].
    Baixo lsr_n = retail excessivamente short = potencial short squeeze (sinal LONG)."""

    adx_n: Optional[float] = None
    """ADX(14) normalizado via /100 → [0, 1].
    Alto adx_n = tendência forte = sinal de momentum mais confiável."""

    kal: Optional[float] = None
    """Componente Kalman: desvio normalizado do preço em relação ao estado estimado.
    Positivo = preço acima da estimativa Kalman = impulso bullish.
    Usado apenas em Tier3 quando kal_active=True."""

    kal_active: bool = False
    """True quando R²_rolling(500c) < 0.30.
    Kalman é útil em regimes não-lineares (baixo R² = alta divergência)."""

    kal_r2: Optional[float] = None
    """R² rolling atual (500 candles). Referência para monitoramento."""

    # ── Features para gerenciamento de risco (valores brutos) ─────────────

    atr14: Optional[float] = None
    """ATR(14) em preço absoluto. Usado para cálculo do SL: entry - 2×ATR."""

    rsi: Optional[float] = None
    """RSI(14) em escala 0–100. Usado no gatilho secundário (RSI < 30)."""

    close: float = 0.0
    """Preço de fechamento do candle."""

    # ── Regime OI ─────────────────────────────────────────────────────────

    oi_regime: OIRegime = OIRegime.NEUTRO
    """Regime de Open Interest classificado para este candle."""

    oi_mult: float = 1.0
    """Multiplicador de score derivado do regime OI. Ver OI_MULTIPLIERS."""

    # ── Score calculado pelo Signal Engine ────────────────────────────────

    score: float = 0.0
    """Score final após aplicação da fórmula de tier e oi_mult.
    Preenchido pelo SignalEngine após receber este FeatureSet."""

    # ── Metadados ─────────────────────────────────────────────────────────

    zvol_raw: float = 0.0
    """Z-score bruto (não normalizado via tanh) — usado no gatilho secundário."""

    def is_ready(self) -> bool:
        """
        Retorna True se todas as features obrigatórias estão disponíveis.
        Features None indicam janela de aquecimento insuficiente.
        """
        required = [self.zvol_n, self.cvd_n, self.lsr_n, self.adx_n, self.atr14, self.rsi]
        return all(v is not None for v in required)

    def __repr__(self) -> str:
        return (
            f"FeatureSet({self.symbol} T{self.tier} | "
            f"score={self.score:.3f} | "
            f"zvol={self.zvol_n:.3f} cvd={self.cvd_n:.3f} "
            f"lsr={self.lsr_n:.3f} adx={self.adx_n:.3f} | "
            f"oi={self.oi_regime.value}×{self.oi_mult} | "
            f"atr={self.atr14:.4f} rsi={self.rsi:.1f})"
        )
