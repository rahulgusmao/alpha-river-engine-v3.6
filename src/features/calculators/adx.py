"""
src/features/calculators/adx.py
ADX(14) + ATR(14) usando Wilder's Smoothing Method.

ADX mede a FORÇA da tendência (não a direção). Valores altos → tendência forte.
ATR(14) é um subproduto natural do cálculo do ADX e é exportado para uso no SL.

Algoritmo (Wilder, 1978):
─────────────────────────
1. True Range (TR):
       TR = max(high - low, |high - prev_close|, |low - prev_close|)

2. Directional Movement:
       +DM = max(high - prev_high, 0)  se (high - prev_high) > (prev_low - low) else 0
       -DM = max(prev_low - low, 0)    se (prev_low - low) > (high - prev_high) else 0
       (ambos zero quando empate ou ambos negativos)

3. Wilder's Smoothing (período N):
       Primeira: média simples dos primeiros N valores
       Demais:   smoothed = prev_smoothed - (prev_smoothed / N) + novo_valor

4. Indicadores Direcionais:
       +DI = 100 × smooth(+DM) / smooth(TR)
       -DI = 100 × smooth(-DM) / smooth(TR)

5. Índice Direcional (DX):
       DX = 100 × |+DI - -DI| / (+DI + -DI)

6. ADX = Wilder's Smoothing(DX, N)

Normalização:
       adx_n = ADX / 100   → [0, 1]
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

_PERIOD = 14
_EPSILON = 1e-10


@dataclass
class _ADXState:
    """Estado de Wilder's smoothing por símbolo."""
    # Buffers para os primeiros N períodos (fase de inicialização SMA)
    tr_init:   List[float] = field(default_factory=list)
    pdm_init:  List[float] = field(default_factory=list)
    ndm_init:  List[float] = field(default_factory=list)
    dx_init:   List[float] = field(default_factory=list)

    # Valores smoothed (após fase de inicialização)
    atr:       float = 0.0
    pdm_s:     float = 0.0    # +DM smoothed
    ndm_s:     float = 0.0    # -DM smoothed
    adx:       float = 0.0
    dx_ready:  bool  = False   # True após _PERIOD de DX acumulados

    # Candle anterior (necessário para DM e TR)
    prev_high:  float = 0.0
    prev_low:   float = 0.0
    prev_close: float = 0.0
    has_prev:   bool  = False


class ADXCalculator:
    """
    Calcula ADX(14) e ATR(14) incrementalmente com Wilder's Smoothing.
    Produz adx_n (ADX/100) e atr14 (em preço absoluto).
    """

    def __init__(self, period: int = _PERIOD):
        self._period = period
        self._states: Dict[str, _ADXState] = {}

    # ──────────────────────────── INICIALIZAÇÃO ────────────────────────────

    def initialize(
        self,
        symbol: str,
        highs:  List[float],
        lows:   List[float],
        closes: List[float],
    ) -> None:
        """
        Inicializa o estado processando o histórico de candles sequencialmente.
        Necessário para warm-up antes do streaming ao vivo.

        Args:
            highs, lows, closes: listas de mesmo tamanho, ordem cronológica
        """
        self._states[symbol] = _ADXState()
        for i in range(len(closes)):
            self._update_state(symbol, highs[i], lows[i], closes[i])

    # ──────────────────────────── UPDATE ───────────────────────────────────

    def update(
        self,
        symbol: str,
        high:   float,
        low:    float,
        close:  float,
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        Processa um novo candle e retorna (adx_n, atr14).

        Returns:
            (adx_n, atr14): ambos None se ainda não há dados suficientes.
            adx_n em [0, 1], atr14 em preço absoluto.
        """
        if symbol not in self._states:
            self._states[symbol] = _ADXState()

        self._update_state(symbol, high, low, close)
        st = self._states[symbol]

        if not st.dx_ready:
            return None, None

        adx_n = st.adx / 100.0
        return float(np.clip(adx_n, 0.0, 1.0)), float(st.atr)

    # ──────────────────────────── LÓGICA INTERNA ───────────────────────────

    def _update_state(
        self,
        symbol: str,
        high:   float,
        low:    float,
        close:  float,
    ) -> None:
        """Atualiza o estado do Wilder's smoothing para o símbolo."""
        st = self._states[symbol]

        if not st.has_prev:
            # Primeiro candle: apenas armazena como referência
            st.prev_high  = high
            st.prev_low   = low
            st.prev_close = close
            st.has_prev   = True
            return

        # ── Calcula TR, +DM, -DM ─────────────────────────────────────────
        tr = max(
            high - low,
            abs(high - st.prev_close),
            abs(low  - st.prev_close),
        )

        up_move   = high - st.prev_high
        down_move = st.prev_low - low

        if up_move > down_move and up_move > 0:
            pdm = up_move
        else:
            pdm = 0.0

        if down_move > up_move and down_move > 0:
            ndm = down_move
        else:
            ndm = 0.0

        # ── Fase de inicialização: acumula os primeiros `period` valores ──
        if len(st.tr_init) < self._period:
            st.tr_init.append(tr)
            st.pdm_init.append(pdm)
            st.ndm_init.append(ndm)

            if len(st.tr_init) == self._period:
                # Primeiro ATR/+DM/-DM = SMA simples dos primeiros N períodos
                st.atr   = sum(st.tr_init)  / self._period
                st.pdm_s = sum(st.pdm_init) / self._period
                st.ndm_s = sum(st.ndm_init) / self._period

        else:
            # ── Fase estável: Wilder's smoothing ─────────────────────────
            st.atr   = st.atr   - (st.atr   / self._period) + tr
            st.pdm_s = st.pdm_s - (st.pdm_s / self._period) + pdm
            st.ndm_s = st.ndm_s - (st.ndm_s / self._period) + ndm

            # ── Calcula +DI, -DI, DX ─────────────────────────────────────
            if st.atr < _EPSILON:
                pdi = 0.0
                ndi = 0.0
            else:
                pdi = 100.0 * st.pdm_s / st.atr
                ndi = 100.0 * st.ndm_s / st.atr

            di_sum  = pdi + ndi
            di_diff = abs(pdi - ndi)
            dx = 100.0 * di_diff / (di_sum + _EPSILON)

            # ── Fase de inicialização do ADX: acumula DX ─────────────────
            if not st.dx_ready:
                st.dx_init.append(dx)
                if len(st.dx_init) == self._period:
                    # Primeiro ADX = SMA simples dos primeiros N DX
                    st.adx     = sum(st.dx_init) / self._period
                    st.dx_ready = True
            else:
                # ADX via Wilder's smoothing
                st.adx = st.adx - (st.adx / self._period) + dx

        # Atualiza candle anterior
        st.prev_high  = high
        st.prev_low   = low
        st.prev_close = close
