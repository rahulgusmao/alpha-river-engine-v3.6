"""
src/risk/stop_loss.py
Cálculo do Stop Loss inicial — configuração canônica v3.2.

Fórmula:
    sl_price = entry_price - sl_atr_multiplier × ATR(14)
    (padrão: entry_price - 2 × ATR(14))

O SL é calculado UMA VEZ no momento da entrada e pode ser
atualizado apenas PARA CIMA pelo trailing stop (nunca recua).
"""


class StopLossCalculator:
    """
    Calcula o SL inicial para uma posição LONG.
    Stateless.
    """

    def __init__(self, atr_multiplier: float = 2.0):
        """
        Args:
            atr_multiplier: multiplicador do ATR para o SL (default: 2.0)
        """
        self._mult = atr_multiplier

    def compute(self, entry_price: float, atr14: float) -> float:
        """
        Calcula o preço de stop loss inicial.

        Args:
            entry_price: preço de entrada (close do candle de gatilho)
            atr14:       ATR(14) em preço absoluto

        Returns:
            sl_price: preço de stop loss (sempre < entry_price para LONG).
                      Floor de 5% do entry_price para tokens de baixo preço
                      onde ATR pode ser maior que entry (e.g. memecoins de $0.01).
        """
        sl_raw = entry_price - self._mult * atr14
        # Floor: SL nunca menor que 5% do entry. Sem esse piso, tokens muito
        # baratos geram sl_price ≤ 0, que o candle.low nunca atinge → posição
        # nunca fecha pelo SL e acumula indefinidamente.
        sl_floor = entry_price * 0.05
        return float(max(sl_raw, sl_floor))
