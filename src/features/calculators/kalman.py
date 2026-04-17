"""
src/features/calculators/kalman.py
Filtro de Kalman univariado em RETORNOS + R² rolling(500c) para critério de ativação.

Por que retornos e não preços?
  Preços de crypto são não-estacionários (random walk com drift).
  Aplicar Kalman em série não-estacionária produz R² artificialmente alto
  (o filtro "aprende" o drift, não a estrutura), tornando kal_active=False
  mesmo em regimes de alta volatilidade onde o sinal tem valor.
  Retornos (r = close/close[-1] - 1) são estacionários de primeira ordem
  — o filtro estima o componente "suave" dos retornos, e a inovação capta
  surpresas de momentum com significância estatística real.

Modelo de espaço de estados (random walk em retornos):
    Estado:      x_t  = x_{t-1} + w_t       w_t ~ N(0, Q)
    Observação:  z_t  = x_t + v_t            v_t ~ N(0, R)

    x_t é o "retorno suavizado" estimado no candle t.
    Inovação = retorno observado − retorno estimado.

Equações de Kalman (1D simplificado):
    Predição:
        x_pred = x_est                       (transição identidade)
        P_pred = P + Q                       (covariância predita)

    Update:
        K      = P_pred / (P_pred + R)       (Kalman gain)
        innov  = r_t - x_pred                (inovação)
        x_est  = x_pred + K × innov          (estado atualizado)
        P      = (1 - K) × P_pred            (covariância atualizada)

Feature `kal`:
    kal = tanh(innov / (std_innov_rolling + ε))   → [-1, 1]

    Positivo: retorno acima do estimado → impulso bullish não esperado
    Negativo: retorno abaixo → surpresa negativa

Critério de ativação (configuração canônica):
    kal_active = R²_rolling(500c) < 0.30

    R² em retornos baixo → estrutura de correlação serial fraca →
    regime de surpresas → inovações têm valor preditivo.

    R² alto  → retornos têm padrão repetitivo que o filtro captura →
    inovações são ruído puro (Kalman não acrescenta).
"""

from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

# Parâmetros do modelo Kalman (para retornos de crypto 15m)
_Q_DEFAULT    = 1e-6   # ruído de processo — retornos derivam lentamente
_R_DEFAULT    = 1e-4   # ruído de observação — std² de retornos ~0.002²
_P0_DEFAULT   = 1e-4   # covariância inicial (incerteza inicial calibrada)

# Janela do R² rolling (critério de ativação do Kalman)
_R2_WINDOW    = 500
_R2_THRESHOLD = 0.30

# Janela da std rolling de inovações (para normalização do kal)
_INNOV_STD_WINDOW = 100

_EPSILON = 1e-10


@dataclass
class _KalmanState:
    """Estado do filtro Kalman por símbolo (operando em retornos)."""
    x_est:        float = 0.0    # estimativa do retorno suavizado
    P:            float = 1e-4   # covariância do erro de estimativa
    initialized:  bool  = False
    prev_close:   Optional[float] = None  # último close para calcular próximo retorno

    # Histórico para R² rolling — armazena RETORNOS (não preços)
    returns_hist: deque = field(default_factory=lambda: deque(maxlen=_R2_WINDOW))
    x_est_hist:   deque = field(default_factory=lambda: deque(maxlen=_R2_WINDOW))

    # Histórico de inovações para normalização da feature kal
    innov_hist:   deque = field(default_factory=lambda: deque(maxlen=_INNOV_STD_WINDOW))


class KalmanCalculator:
    """
    Filtro de Kalman univariado em retornos com critério de ativação via R² rolling.
    Usado apenas em Tier3. Tier1/2 não usa Kalman (experimentos P33/P34).
    """

    def __init__(
        self,
        q:            float = _Q_DEFAULT,
        r:            float = _R_DEFAULT,
        p0:           float = _P0_DEFAULT,
        r2_window:    int   = _R2_WINDOW,
        r2_threshold: float = _R2_THRESHOLD,
    ):
        """
        Args:
            q:            ruído de processo — controla quanto x_est pode "pular"
            r:            ruído de observação — controla suavidade do filtro
            p0:           covariância inicial
            r2_window:    janela rolling para cálculo do R²
            r2_threshold: limiar de ativação (kal_active = R² < threshold)
        """
        self._q            = q
        self._r            = r
        self._p0           = p0
        self._r2_window    = r2_window
        self._r2_threshold = r2_threshold
        self._states: Dict[str, _KalmanState] = {}

    # ──────────────────────────── INICIALIZAÇÃO ────────────────────────────

    def initialize(self, symbol: str, closes: List[float]) -> None:
        """
        Aquece o filtro processando histórico de closes como retornos.

        Converte closes → retornos internamente:
            r[i] = closes[i] / closes[i-1] - 1   (i ≥ 1)

        O filtro processa cada retorno sequencialmente para que o R² rolling
        tenha dados suficientes no startup.

        Args:
            symbol: símbolo
            closes: lista de closes históricos (pelo menos 2 elementos)
        """
        if len(closes) < 2:
            return

        state = _KalmanState(P=self._p0)
        self._states[symbol] = state

        # Processa cada retorno sequencialmente
        for i in range(1, len(closes)):
            prev = closes[i - 1]
            curr = closes[i]
            if prev > 0.0:
                ret = curr / prev - 1.0
            else:
                ret = 0.0
            self._step(state, ret)

        # Armazena o último close para calcular o próximo retorno em update()
        state.prev_close = float(closes[-1])

    # ──────────────────────────── UPDATE ───────────────────────────────────

    def update(
        self,
        symbol: str,
        close:  float,
    ) -> Tuple[Optional[float], bool, Optional[float]]:
        """
        Processa novo close: converte para retorno e executa passo Kalman.

        Returns:
            kal:        feature normalizada em [-1, 1], None se insuficiente
            kal_active: True se R² < threshold (Kalman deve ser usado)
            r2:         R² rolling atual (None se < r2_window retornos)
        """
        if symbol not in self._states:
            state = _KalmanState(P=self._p0)
            self._states[symbol] = state
        else:
            state = self._states[symbol]

        # Primeiro close visto: só armazena, não há retorno ainda
        if state.prev_close is None or state.prev_close <= 0.0:
            state.prev_close = close
            return None, False, None

        ret = close / state.prev_close - 1.0
        state.prev_close = close

        return self._step(state, ret)

    # ──────────────────────────── PASSO KALMAN ─────────────────────────────

    def _step(
        self,
        state: _KalmanState,
        ret:   float,
    ) -> Tuple[Optional[float], bool, Optional[float]]:
        """
        Executa um passo do filtro Kalman com retorno `ret` como observação.
        Retorna (kal, kal_active, r2).
        """

        if not state.initialized:
            # Inicializa estado com o primeiro retorno observado
            state.x_est       = ret
            state.P           = self._p0
            state.initialized = True
            state.returns_hist.append(ret)
            state.x_est_hist.append(ret)
            return None, False, None

        # ── Passo de predição ─────────────────────────────────────────────
        x_pred = state.x_est           # random walk: previsão = estado anterior
        P_pred = state.P + self._q     # covariância predita aumenta pelo ruído do processo

        # ── Passo de atualização ──────────────────────────────────────────
        K           = P_pred / (P_pred + self._r)   # Kalman gain
        innovation  = ret - x_pred                   # inovação = surpresa no retorno
        state.x_est = x_pred + K * innovation        # estado atualizado
        state.P     = (1.0 - K) * P_pred             # covariância atualizada

        # ── Registra histórico ────────────────────────────────────────────
        state.returns_hist.append(ret)
        state.x_est_hist.append(state.x_est)
        state.innov_hist.append(innovation)

        # ── Calcula R² rolling em retornos ────────────────────────────────
        r2: Optional[float] = None
        kal_active = False

        if len(state.returns_hist) >= self._r2_window:
            r_arr = np.array(state.returns_hist, dtype=np.float64)
            e_arr = np.array(state.x_est_hist,   dtype=np.float64)

            ss_res = np.sum((r_arr - e_arr) ** 2)
            ss_tot = np.sum((r_arr - r_arr.mean()) ** 2)

            r2 = float(1.0 - ss_res / (ss_tot + _EPSILON))
            r2 = float(np.clip(r2, -1.0, 1.0))   # R² pode ser negativo se modelo ruim
            kal_active = r2 < self._r2_threshold

        # ── Calcula feature kal normalizada ───────────────────────────────
        kal: Optional[float] = None
        if len(state.innov_hist) >= 20:
            innov_arr = np.array(state.innov_hist, dtype=np.float64)
            innov_std = innov_arr.std()
            kal = float(np.tanh(innovation / (innov_std + _EPSILON)))

        return kal, kal_active, r2
