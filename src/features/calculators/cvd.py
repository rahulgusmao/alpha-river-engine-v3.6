"""
src/features/calculators/cvd.py
CVD Delta normalizado (Cumulative Volume Delta por barra).

O CVD delta de cada barra é:
    cvd_delta = taker_buy_base_vol - taker_sell_base_vol
              = V - (volume - V)
              = 2V - volume

Normalização:
    cvd_n = tanh(cvd_delta / (std(cvd_window) + ε))   → [-1, 1]

Usar a std rolling como denominador (em vez de um valor fixo) torna a
normalização adaptativa ao regime de volatilidade do token.

Sinal:
    cvd_n > 0 → takers comprando mais do que vendendo → pressão compradora
    cvd_n < 0 → takers vendendo mais do que comprando → pressão vendedora
"""

from collections import deque
from typing import Dict, Optional

import numpy as np

_EPSILON = 1e-10


class CVDCalculator:
    """
    Computa CVD delta normalizado por barra para um conjunto de símbolos.
    """

    def __init__(self, window: int = 100, min_periods: int = 20):
        """
        Args:
            window:      janela rolling para cálculo da std de normalização
            min_periods: mínimo de candles para retornar valor válido
        """
        self._window = window
        self._min_periods = min_periods
        self._buffers: Dict[str, deque] = {}   # histórico de cvd_delta

    def initialize(self, symbol: str, cvd_deltas: list) -> None:
        """
        Pré-carrega buffer com histórico de cvd_deltas calculados externamente.

        Args:
            cvd_deltas: lista de floats (taker_buy_vol - taker_sell_vol por candle)
        """
        buf = deque(maxlen=self._window)
        for d in cvd_deltas[-self._window:]:
            buf.append(float(d))
        self._buffers[symbol] = buf

    def update(
        self,
        symbol: str,
        taker_buy_base_vol: float,
        total_volume: float,
    ) -> Optional[float]:
        """
        Atualiza o buffer com o novo candle e retorna cvd_n normalizado.

        Args:
            taker_buy_base_vol: campo V do kline (takerBuyBaseAssetVolume)
            total_volume:       campo v do kline (volume total)

        Returns:
            cvd_n em [-1, 1], ou None se janela insuficiente.
        """
        cvd_delta = taker_buy_base_vol - (total_volume - taker_buy_base_vol)

        if symbol not in self._buffers:
            self._buffers[symbol] = deque(maxlen=self._window)

        self._buffers[symbol].append(float(cvd_delta))
        buf = self._buffers[symbol]

        if len(buf) < self._min_periods:
            return None

        arr = np.array(buf, dtype=np.float64)
        std = arr.std()

        return float(np.tanh(cvd_delta / (std + _EPSILON)))
