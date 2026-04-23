"""
src/risk/trailing.py
Trailing Stop gain-based — configuração canônica v3.5.

Fórmula (após ativação):
    trail_stop = entry_price + capture_ratio × (peak - entry_price)

Ativação:
    peak / entry_price >= activation_ratio   (padrão: 1.002 → +0.2% de ganho)

Lógica de atualização (chamada a cada candle fechado):
    1. Atualiza peak se current_close > peak
    2. Verifica condição de ativação do trailing
    3. Se ativo: calcula novo trail_stop e aplica floor (nunca recua)
    4. Verifica se current_close <= sl_price atual → sinaliza fechamento

Invariante: sl_price só cresce (trailing nunca recua o stop).

Configuração canônica (v3.5 — grid search Jan-Fev 2026, 25 combos):
    activation_ratio = 1.002   (+0.2% de ganho mínimo para ativar)
    capture_ratio    = 0.90    (captura 90% do move desde o entry)

Histórico:
    v3.2: activation=1.003, capture=0.80
    v3.5: activation=1.002, capture=0.90 (PF 0.837→0.870)
"""

from src.risk.models import CloseReason, Position
from typing import Optional


class TrailingStop:
    """
    Aplica a lógica de trailing stop a uma posição aberta.
    Stateless — recebe Position e retorna atualizações.
    """

    def __init__(
        self,
        activation_ratio: float = 1.002,
        capture_ratio:    float = 0.90,
    ):
        """
        Args:
            activation_ratio: peak/entry mínimo para ativar trailing (1.002 = +0.2%)
            capture_ratio:    fração do move capturada pelo stop (0.90 = 90%)
        """
        self._activation = activation_ratio
        self._capture    = capture_ratio

    def update(self, position: Position, current_close: float) -> Optional[CloseReason]:
        """
        Atualiza o peak e o sl_price da posição com base no close atual.
        Modifica `position` in-place: atualiza peak, sl_price, trailing_active.

        Args:
            position:      posição aberta (modificada in-place)
            current_close: preço de fechamento do candle atual

        Returns:
            CloseReason.TRAILING se o close atual atingiu o trailing stop.
            None se a posição deve permanecer aberta.
        """
        # 1. Atualiza peak
        if current_close > position.peak:
            position.peak = current_close

        # 2. Verifica condição de ativação do trailing
        gain_ratio = position.peak / position.entry_price
        if gain_ratio >= self._activation:
            position.trailing_active = True

        # 3. Calcula novo trailing stop (apenas se ativo)
        if position.trailing_active:
            new_trail = position.entry_price + self._capture * (
                position.peak - position.entry_price
            )
            # Floor: SL nunca recua (só cresce)
            if new_trail > position.sl_price:
                position.sl_price = new_trail

        # 4. Verifica se o preço atingiu o SL atual (inicial ou trailing)
        if current_close <= position.sl_price:
            reason = (
                CloseReason.TRAILING if position.trailing_active else CloseReason.SL
            )
            return reason

        return None

    def describe(self, position: Position) -> dict:
        """Retorna estado do trailing para logging."""
        gain_ratio = position.peak / position.entry_price if position.entry_price else 0
        return {
            "symbol":          position.symbol,
            "entry":           round(position.entry_price, 4),
            "peak":            round(position.peak, 4),
            "sl_current":      round(position.sl_price, 4),
            "gain_ratio":      round(gain_ratio, 5),
            "trailing_active": position.trailing_active,
        }
