"""
src/features/calculators/oi_regime.py
Classificação de regime de Open Interest e multiplicador de score.

Lógica de classificação (configuração canônica v3.2):
────────────────────────────────────────────────────
    oi_delta_pct = (oi_atual - oi_anterior) / oi_anterior

    LONG_BUILD  : oi_delta_pct > +threshold AND price_up (close > prev_close)
                  → OI crescendo + preço subindo = compradores entrando
                  → mult = 1.5

    SHORT_BUILD : oi_delta_pct > +threshold AND price_down (close ≤ prev_close)
                  → OI crescendo + preço caindo = vendedores entrando
                  → mult = 0.8 (sinal desfavorável para LONG)

    DELEV       : oi_delta_pct < -threshold
                  → OI caindo = desalavancagem, liquidações, saída do mercado
                  → mult = 0.5 (risk-off, reduz exposição)

    NEUTRO      : |oi_delta_pct| ≤ threshold
                  → sem variação significativa
                  → mult = 1.0

Threshold inicial: 0.01 (1% de variação de OI por candle).
A calibrar empiricamente em paper trading.

Nota: o threshold deve ser avaliado por tier ou por família de tokens
(BTC/ETH provavelmente precisam de threshold menor do que altcoins de menor OI).
Implementação atual usa threshold global único para simplicidade.
"""

from typing import Dict, Optional, Tuple

from src.features.models import OIRegime, OI_MULTIPLIERS

_EPSILON = 1e-10


class OIRegimeClassifier:
    """
    Classifica o regime de OI e retorna o multiplicador correspondente.
    Mantém o OI do candle anterior por símbolo.
    """

    def __init__(self, threshold_pct: float = 0.01):
        """
        Args:
            threshold_pct: variação mínima de OI (em % do OI anterior)
                           para classificar como BUILD ou DELEV (default: 1%).
        """
        self._threshold = threshold_pct
        self._prev_oi:    Dict[str, float] = {}
        self._prev_close: Dict[str, float] = {}

    def initialize(
        self,
        symbol:  str,
        oi_list: list,
        closes:  list,
    ) -> None:
        """
        Pré-carrega o último par (oi, close) do histórico para o símbolo.

        Args:
            oi_list: lista de floats (open_interest por candle, do mais antigo ao mais novo)
            closes:  lista de floats (close por candle, mesma ordem)
        """
        if oi_list:
            self._prev_oi[symbol] = float(oi_list[-1])
        if closes:
            self._prev_close[symbol] = float(closes[-1])

    def update(
        self,
        symbol: str,
        oi:     Optional[float],
        close:  float,
    ) -> Tuple[OIRegime, float]:
        """
        Classifica o regime do candle atual.

        Args:
            oi:    Open Interest atual (pode ser None se fetch falhou)
            close: preço de fechamento atual

        Returns:
            (regime, multiplier)
            Se oi for None ou não houver OI anterior, retorna NEUTRO (mult=1.0).
        """
        regime = OIRegime.NEUTRO

        if oi is not None and symbol in self._prev_oi:
            prev_oi    = self._prev_oi[symbol]
            prev_close = self._prev_close.get(symbol, close)

            if prev_oi > _EPSILON:
                delta_pct = (oi - prev_oi) / prev_oi
                price_up  = close > prev_close

                if delta_pct > self._threshold:
                    regime = OIRegime.LONG_BUILD if price_up else OIRegime.SHORT_BUILD
                elif delta_pct < -self._threshold:
                    regime = OIRegime.DELEV

        # Atualiza estado anterior
        if oi is not None:
            self._prev_oi[symbol] = oi
        self._prev_close[symbol] = close

        return regime, OI_MULTIPLIERS[regime]
