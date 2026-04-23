"""
src/features/calculators/lsr.py
Long/Short Ratio normalizado (varejo global).

Normalização min-max rolling:
    lsr_n = (lsr - min(window)) / (max(window) - min(window) + ε)   → [0, 1]

Interpretação no contexto do score LONG:
    LSR alto  → mais contas long do que short no varejo
    LSR baixo → mais contas short do que long no varejo → potencial short squeeze

Nota: o peso de lsr_n na fórmula (0.10 em Tier1/2, 0.07 em Tier3) é relativamente
baixo, indicando que LSR é um fator de confirmação, não o driver principal do sinal.
A direção da normalização (lsr alto = lsr_n alto vs baixo = lsr_n alto) deve ser
validada em paper trading. A implementação atual usa min-max direto (LSR alto → lsr_n alto).
"""

from collections import deque
from typing import Dict, Optional

import numpy as np

_EPSILON = 1e-10


class LSRCalculator:
    """
    Computa Long/Short Ratio normalizado via min-max rolling.
    """

    def __init__(self, window: int = 100, min_periods: int = 20):
        """
        Args:
            window:      janela rolling para min/max de normalização
            min_periods: mínimo de candles para retornar valor válido
        """
        self._window = window
        self._min_periods = min_periods
        self._buffers: Dict[str, deque] = {}

    def initialize(self, symbol: str, lsr_values: list) -> None:
        """
        Pré-carrega buffer com histórico de ratios LSR.

        Args:
            lsr_values: lista de floats (longShortRatio por período)
        """
        buf = deque(maxlen=self._window)
        for v in lsr_values[-self._window:]:
            buf.append(float(v))
        self._buffers[symbol] = buf

    def update(self, symbol: str, lsr: float) -> Optional[float]:
        """
        Atualiza o buffer e retorna lsr_n normalizado.

        Args:
            lsr: valor de longShortRatio (ex: 1.25 = 1.25 longs para cada short)

        Returns:
            lsr_n em [0, 1], ou None se janela insuficiente.
        """
        if symbol not in self._buffers:
            self._buffers[symbol] = deque(maxlen=self._window)

        self._buffers[symbol].append(float(lsr))
        buf = self._buffers[symbol]

        if len(buf) < self._min_periods:
            return None

        arr = np.array(buf, dtype=np.float64)
        lo  = arr.min()
        hi  = arr.max()

        if (hi - lo) < 1e-9:
            return 0.5  # sem range → valor neutro

        return float((lsr - lo) / (hi - lo))
