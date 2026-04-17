"""
src/features/calculators/zvol.py
Z-Score de Volume Financeiro normalizado.

Fórmula (conforme AF):
    vol_fin     = volume × close              (volume financeiro em USDT)
    log_vol     = log(vol_fin)                (log para estacionarizar)
    z           = (log_vol - mean(window)) / std(window)
    zvol_n      = tanh(z / 2)                → [-1, 1]

O log é essencial porque vol_fin segue distribuição log-normal.
Z-score no espaço linear capturaria assimetria e outliers graves.

zvol_raw (z-score puro) é retornado para uso no gatilho secundário (z > 1.5σ).
"""

from collections import deque
from typing import Dict, Optional, Tuple

import numpy as np


class ZVolCalculator:
    """
    Mantém uma janela rolling de log(volume financeiro) por símbolo
    e computa Z-Score normalizado.
    Stateful: um buffer de deque por símbolo.
    """

    def __init__(self, window: int = 100, min_periods: int = 20):
        """
        Args:
            window:      tamanho da janela rolling (candles)
            min_periods: mínimo de candles antes de retornar valor válido
        """
        self._window     = window
        self._min_periods = min_periods
        self._buffers: Dict[str, deque] = {}

    def initialize(self, symbol: str, volumes: list, closes: list) -> None:
        """
        Pré-carrega o buffer com histórico de log(volume financeiro).
        Chame durante o warm-up antes do streaming ao vivo.

        Args:
            symbol:  símbolo
            volumes: lista de volumes base (ex: BTC)
            closes:  lista de closes (mesmo tamanho de volumes)
        """
        buf = deque(maxlen=self._window)

        # Usa somente os últimos `window` candles
        paired = list(zip(volumes, closes))[-self._window:]
        for v, c in paired:
            vol_fin = float(v) * float(c)
            if vol_fin > 0.0:
                buf.append(np.log(vol_fin))

        self._buffers[symbol] = buf

    def update(
        self,
        symbol: str,
        volume: float,
        close:  float,
    ) -> Tuple[Optional[float], float]:
        """
        Atualiza o buffer com log(volume financeiro) e retorna (zvol_n, zvol_raw).

        Args:
            symbol: símbolo
            volume: volume base do candle fechado
            close:  preço de fechamento do candle

        Returns:
            (zvol_n, zvol_raw):
              zvol_n   ∈ [-1, 1] via tanh(z/2), None se janela insuficiente
              zvol_raw = z-score puro (σ)
        """
        if symbol not in self._buffers:
            self._buffers[symbol] = deque(maxlen=self._window)

        vol_fin = volume * close
        if vol_fin <= 0.0:
            # Volume/close inválido — não atualiza buffer, retorna neutro
            return None, 0.0

        log_vol = np.log(vol_fin)
        self._buffers[symbol].append(log_vol)
        buf = self._buffers[symbol]

        if len(buf) < self._min_periods:
            return None, 0.0

        arr  = np.array(buf, dtype=np.float64)
        mean = arr.mean()
        std  = arr.std()

        if std < 1e-10:
            return 0.0, 0.0

        z      = (log_vol - mean) / std
        zvol_n = float(np.tanh(z / 2.0))
        return zvol_n, float(z)
