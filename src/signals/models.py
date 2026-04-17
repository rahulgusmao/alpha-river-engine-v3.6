"""
src/signals/models.py
Modelos de saída do Signal Engine.

Signal é o contrato de interface entre o Signal Engine e o Risk Manager.
Representa uma oportunidade de entrada validada — score acima do threshold
ou condição de gatilho secundário atingida.
"""

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.features.models import FeatureSet


class TriggerType(str, Enum):
    """
    Tipo de gatilho que originou o sinal de entrada.

    PRIMARY:   score >= threshold (0.40) — sinal institucional forte
    SECONDARY: RSI < 30 AND zvol_raw > 1.5σ — oversold com volume anômalo
               (potencial exaustão de venda + absorção institucional)
    """
    PRIMARY   = "PRIMARY"
    SECONDARY = "SECONDARY"


@dataclass
class Signal:
    """
    Sinal de entrada validado para um símbolo.

    Produzido pelo SignalEngine e consumido pelo RiskManager.
    Contém tudo que o RiskManager precisa para montar o OrderSpec:
    entry_price, atr14, oi_regime, score e tier.

    direction: apenas 'LONG' nesta fase (SHORT não implementado).
    """

    symbol:       str
    ts_close:     int         # epoch ms — timestamp do candle que gerou o sinal
    tier:         int         # 1, 2 ou 3
    score:        float       # score calculado (preenchido pelo Scorer)
    direction:    str         # 'LONG'
    entry_price:  float       # preço de fechamento do candle de gatilho
    atr14:        float       # ATR(14) em preço — para SL = entry - 2×ATR
    oi_regime:    str         # nome do regime OI (ex: 'LONG_BUILD')
    oi_mult:      float       # multiplicador aplicado ao score
    trigger_type: TriggerType # PRIMARY ou SECONDARY
    feature_set:  "FeatureSet" = None  # referência completa para rastreabilidade

    def __repr__(self) -> str:
        return (
            f"Signal({self.symbol} T{self.tier} | "
            f"{self.trigger_type.value} | "
            f"score={self.score:.3f} | "
            f"entry={self.entry_price:.4f} | "
            f"atr={self.atr14:.4f} | "
            f"oi={self.oi_regime}×{self.oi_mult})"
        )
