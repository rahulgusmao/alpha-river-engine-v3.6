"""
src/risk/kill_switch.py
Kill Switch — proteção de portfólio em condições sistêmicas adversas.

Dois gatilhos independentes (OR) — configuração canônica v3.2:
────────────────────────────────────────────────────────────────
BTCD_DROP:   BTC Dominance caiu > X% em Y horas
             (padrão: -3% em 4h = 16 candles de 15m)
             → Sinal de rotação de capital para BTC / risk-off sistêmico.

DD_BREACH:   Drawdown do portfólio desde o pico > Z%
             (padrão: 10%)
             → Proteção de capital — encerra exposição antes de perda catastrófica.
────────────────────────────────────────────────────────────────

Ações ao ativar:
  1. Fechar TODAS as posições abertas (via ExecutionEngine)
  2. Bloquear abertura de novas posições por `cooldown_hours`
  3. Logar evento com razão de ativação

Nota sobre BTC.D:
  Os valores de BTC.D chegam com ~5min de delay (CoinGecko Free Tier).
  O lookback de 4h é avaliado sobre os últimos `lookback_candles` valores
  disponíveis no buffer. Isso é aceitável para proteção de portfólio —
  não é um sinal de entrada.
"""

import time
from collections import deque
from typing import Optional

import structlog

logger = structlog.get_logger(__name__)

# Candles de 15m equivalentes a 4 horas
_4H_IN_15M_CANDLES = 16


class KillSwitch:
    """
    Avalia as condições do Kill Switch a cada novo dado disponível.

    Deve ser consultado:
      - A cada novo valor de BTC.D (a cada ~5 min)
      - A cada candle fechado (para verificar drawdown)
      - Antes de processar qualquer novo Signal
    """

    def __init__(
        self,
        btcd_drop_pct:          float = 3.0,
        btcd_lookback_hours:    float = 4.0,
        portfolio_drawdown_pct: float = 0.10,
        cooldown_hours:         float = 4.0,
        kline_interval_min:     int   = 15,
    ):
        """
        Args:
            btcd_drop_pct:          queda de BTC.D em % para ativar (3.0 = 3%)
            btcd_lookback_hours:    janela de lookback para BTC.D (horas)
            portfolio_drawdown_pct: drawdown máximo do portfólio (0.10 = 10%)
            cooldown_hours:         horas de bloqueio após ativação
            kline_interval_min:     intervalo do kline em minutos (15)
        """
        self._btcd_drop_thr  = btcd_drop_pct
        self._dd_thr         = portfolio_drawdown_pct
        self._cooldown_sec   = cooldown_hours * 3600

        # Quantos pontos de BTC.D correspondem ao lookback
        # BTC.D polling = 5 min → lookback 4h = 48 leituras
        btcd_poll_min = 5
        lookback_candles = int((btcd_lookback_hours * 60) / btcd_poll_min)
        self._btcd_buffer: deque = deque(maxlen=lookback_candles + 1)

        # Candles de 15m equivalentes ao lookback de BTC.D
        self._btcd_lookback_candles = int(
            (btcd_lookback_hours * 60) / kline_interval_min
        )

        # Estado do kill switch
        self._active:        bool  = False
        self._active_until:  float = 0.0   # epoch seconds
        self._active_reason: Optional[str] = None

        # Peak de portfólio para cálculo de drawdown
        self._portfolio_peak: float = 0.0

    # ──────────────────────────── ALIMENTAÇÃO DE DADOS ────────────────────

    def update_btcd(self, btcd_value: float) -> None:
        """Alimenta novo valor de BTC.D. Chamar a cada poll do BTCDFetcher."""
        self._btcd_buffer.append(btcd_value)

    def update_portfolio(self, portfolio_value: float) -> None:
        """Alimenta valor atual do portfólio. Atualiza peak se necessário."""
        if portfolio_value > self._portfolio_peak:
            self._portfolio_peak = portfolio_value

    # ──────────────────────────── AVALIAÇÃO ───────────────────────────────

    def is_active(self) -> bool:
        """
        Retorna True se o kill switch está ativo (dentro do cooldown).
        Libera automaticamente após o cooldown.
        """
        if self._active and time.time() > self._active_until:
            self._active = False
            self._active_reason = None
            logger.info("kill_switch_cooldown_expired")
        return self._active

    def evaluate(self, portfolio_value: float) -> bool:
        """
        Avalia ambas as condições. Ativa o kill switch se necessário.

        Deve ser chamado a cada candle fechado.

        Returns:
            True se acabou de ser ativado nesta chamada (novo evento).
            False se já estava ativo ou se nenhuma condição foi atingida.
        """
        if self.is_active():
            return False   # já ativo — não re-ativa

        self.update_portfolio(portfolio_value)

        # ── Condição 1: BTC.D drop ────────────────────────────────────────
        btcd_trigger, btcd_info = self._check_btcd_drop()

        # ── Condição 2: Portfolio drawdown ────────────────────────────────
        dd_trigger, dd_info = self._check_drawdown(portfolio_value)

        if btcd_trigger or dd_trigger:
            reason = btcd_info if btcd_trigger else dd_info
            self._activate(reason)
            return True

        return False

    # ──────────────────────────── CONDIÇÕES ───────────────────────────────

    def _check_btcd_drop(self) -> tuple[bool, str]:
        """Verifica queda de BTC.D no período de lookback."""
        buf = list(self._btcd_buffer)
        if len(buf) < 2:
            return False, ""

        # Compara o valor mais antigo disponível com o mais recente
        oldest = buf[0]
        newest = buf[-1]

        if oldest <= 0:
            return False, ""

        drop_pct = ((newest - oldest) / oldest) * 100.0

        if drop_pct <= -self._btcd_drop_thr:
            info = (
                f"BTC.D caiu {abs(drop_pct):.2f}% "
                f"({oldest:.2f}% → {newest:.2f}%) "
                f"em {len(buf)} leituras"
            )
            return True, info

        return False, ""

    def _check_drawdown(self, portfolio_value: float) -> tuple[bool, str]:
        """Verifica drawdown do portfólio desde o pico."""
        if self._portfolio_peak <= 0:
            return False, ""

        dd = (self._portfolio_peak - portfolio_value) / self._portfolio_peak

        if dd >= self._dd_thr:
            info = (
                f"Drawdown {dd:.2%} desde peak "
                f"(peak={self._portfolio_peak:.2f} atual={portfolio_value:.2f})"
            )
            return True, info

        return False, ""

    def _activate(self, reason: str) -> None:
        """Ativa o kill switch e inicia o cooldown."""
        self._active        = True
        self._active_until  = time.time() + self._cooldown_sec
        self._active_reason = reason

        logger.warning(
            "kill_switch_activated",
            reason=reason,
            cooldown_hours=self._cooldown_sec / 3600,
        )

    # ──────────────────────────── INFO ────────────────────────────────────

    @property
    def reason(self) -> Optional[str]:
        """Razão da última ativação do kill switch."""
        return self._active_reason

    @property
    def btcd_latest(self) -> Optional[float]:
        """Último valor de BTC.D recebido."""
        if self._btcd_buffer:
            return self._btcd_buffer[-1]
        return None
