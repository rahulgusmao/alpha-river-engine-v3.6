"""
src/risk/breakeven.py
Breakeven Stop + MFE Gate — Alpha River v4.0

──────────────────────────────────────────────────────────────────────────────
Histórico de versões
──────────────────────────────────────────────────────────────────────────────
v3.6 (2026-04-13):
  Motivação: análise snapshot live (7.142 trades, 11-13/Abr/2026):
    - 100% dos winners via TRAILING fecham em ≤ 4 candles.
    - Trades pós-c4 sem trailing são zumbis: avg_loss=-1.12%, total=-1.724 USDT.
    - O mecanismo anterior (trigger c8) fechava com losses reais de -8% a -11%.
  Grid search (2.604 trades afetados):
    Cap 0.50% → P&L simulado: +1.127 USDT (ótimo).
  Nova lógica: trigger c4, SL=entry, cap de loss 0.50%.

v4.0 (2026-04-15) — MFE Gate (Q4.1):
  Motivação: backtest P37 Rev. revelou dois problemas opostos:
    1. 57% dos trades breakeven têm MFE=0 desde c1 — nunca subiram.
       São trades estruturalmente ruins que deveriam fechar ANTES do BE c4.
       Desperdiçam capital e aumentam slippage acumulado.
    2. 12.9% dos trades chegam ao regime c6-c9 com WR~100% e PnL 5-10× maior,
       mas são mortos pelo BE c4 indiscriminado — Alpha institucional perdido.

  Solução: dois gates seletivos complementares ao BE padrão:

  Gate 1 — Exit Antecipada (c3, ANTES do BE):
    Condição: MFE_pct < 0.05% AND cvd_n < -0.50
    Ação: fechar posição imediatamente (BREAKEVEN_MFE_EXIT)
    Lógica: trade nunca subiu + fluxo se deteriorando → sair antes de perder mais.
    Impacto esperado: ~57% dos futuros breakevens eliminados com loss menor.

  Gate 2 — Runoff (c4+, OVERRIDE do BE padrão):
    Condição: MFE_pct > 0.50% AND cvd_n >= 0.0
    Ação: NÃO aplicar BE automático neste candle (deixar trailing correr)
    Lógica: trade ganhou tração + fluxo comprador ativo → regime institucional.
    Impacto esperado: ~12.9% dos trades capturam c6-c9 (+1.3% a +3.2%).

  O BE padrão (SL=entry + cap 0.50%) permanece como fallback para todos
  os trades que não se encaixam em nenhum dos dois gates.

──────────────────────────────────────────────────────────────────────────────
Interface pública
──────────────────────────────────────────────────────────────────────────────
  BreakevenConfig: dataclass de configuração (lida de engine.yaml)
  BreakevenStop.check_mfe_exit()   → gate exit antecipada (chamar em c3)
  BreakevenStop.check_mfe_runoff() → gate runoff (chamar em c4+ antes do BE)
  BreakevenStop.check()            → BE padrão (chamar após os dois gates)

Uso pelo RiskManager (ordem obrigatória por candle):
  1. c == 3:  triggered, reason = be.check_mfe_exit(entry, peak, close, cvd_n)
              se triggered → fechar (BREAKEVEN_MFE_EXIT)
  2. c >= 4:  allow_runoff = be.check_mfe_runoff(entry, peak, close, cvd_n)
              se allow_runoff → pular BE padrão neste candle
  3. c >= 4:  new_sl, triggered = be.check(entry, sl, c, trailing_active, close)
              se triggered → fechar (BREAKEVEN ou BREAKEVEN_CAP_LOSS)
"""

from dataclasses import dataclass

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class BreakevenConfig:
    """Configuração do Breakeven Stop + MFE Gate. Lida de risk config no engine.yaml."""

    # BE padrão (v3.6)
    trigger_candles: int   = 4      # candle em que o BE ativa
    loss_cap_pct:    float = 0.50   # % máx de loss abaixo do entry antes de fechar

    # MFE Gate — exit antecipada (v4.0 Q4.1)
    mfe_exit_threshold_pct: float = 0.05   # MFE abaixo → candidato a exit
    mfe_exit_cvd_threshold: float = -0.50  # cvd_n abaixo → confirma exit

    # MFE Gate — runoff (v4.0 Q4.1)
    mfe_runoff_threshold_pct: float = 0.50  # MFE acima → candidato a runoff
    mfe_runoff_cvd_min:       float = 0.00  # cvd_n acima → confirma runoff

    @classmethod
    def from_config(cls, risk_cfg: dict) -> "BreakevenConfig":
        return cls(
            trigger_candles          = risk_cfg.get("breakeven_trigger_candles",    4),
            loss_cap_pct             = risk_cfg.get("breakeven_loss_cap_pct",       0.50),
            mfe_exit_threshold_pct   = risk_cfg.get("mfe_exit_threshold_pct",       0.05),
            mfe_exit_cvd_threshold   = risk_cfg.get("mfe_exit_cvd_threshold",      -0.50),
            mfe_runoff_threshold_pct = risk_cfg.get("mfe_runoff_threshold_pct",     0.50),
            mfe_runoff_cvd_min       = risk_cfg.get("mfe_runoff_cvd_min",           0.00),
        )


class BreakevenStop:
    """
    Gerencia o Breakeven Stop e MFE Gate para posições abertas.

    Stateless — o estado (sl_price, trailing_active, candles_open, peak_price)
    vive no objeto Position. Este módulo apenas decide e aplica a mutação.
    """

    def __init__(self, config: BreakevenConfig) -> None:
        self.cfg = config

    # ──────────────────────────── MFE GATE: EXIT ANTECIPADA ───────────────

    def check_mfe_exit(
        self,
        entry_price:  float,
        peak_price:   float,
        current_close: float,
        cvd_n:        float,
        candles_open: int,
    ) -> bool:
        """
        Gate de exit antecipada — v4.0 Q4.1.

        Chamar no candle 3 (ANTES do BE automático no candle 4).
        Detecta trades que nunca tiveram MFE positivo + fluxo deteriorado.

        Args:
            entry_price:   preço de entrada da posição.
            peak_price:    maior preço desde a entrada (MFE proxy).
            current_close: fechamento do candle atual.
            cvd_n:         CVD normalizado do candle atual ([-1, 1]).
            candles_open:  candles que a posição está aberta.

        Returns:
            True → fechar imediatamente (BREAKEVEN_MFE_EXIT).
            False → não agir (prosseguir para BE padrão ou trailing).
        """
        if candles_open != 3:
            return False

        mfe_pct = (peak_price - entry_price) / entry_price * 100.0

        triggered = (
            mfe_pct < self.cfg.mfe_exit_threshold_pct
            and cvd_n < self.cfg.mfe_exit_cvd_threshold
        )

        if triggered:
            logger.info(
                "breakeven_mfe_exit",
                candles=candles_open,
                entry=round(entry_price, 6),
                peak=round(peak_price, 6),
                mfe_pct=round(mfe_pct, 4),
                cvd_n=round(cvd_n, 4),
            )

        return triggered

    # ──────────────────────────── MFE GATE: RUNOFF ────────────────────────

    def check_mfe_runoff(
        self,
        entry_price:  float,
        peak_price:   float,
        cvd_n:        float,
        candles_open: int,
        trailing_active: bool,
    ) -> bool:
        """
        Gate de runoff — v4.0 Q4.1.

        Chamar no candle 4+ ANTES do BE padrão.
        Detecta trades com tração real + fluxo comprador ativo.
        Se True, o BE padrão NÃO deve ser aplicado neste candle.

        Args:
            entry_price:     preço de entrada da posição.
            peak_price:      maior preço desde a entrada (MFE proxy).
            cvd_n:           CVD normalizado do candle atual.
            candles_open:    candles que a posição está aberta.
            trailing_active: se True, trailing já está gerenciando — runoff irrelevante.

        Returns:
            True → NÃO aplicar BE padrão neste candle (deixar trailing correr).
            False → prosseguir para BE padrão normalmente.
        """
        # Trailing ativo já protege o ganho — runoff é irrelevante
        if trailing_active:
            return False

        if candles_open < self.cfg.trigger_candles:
            return False

        mfe_pct = (peak_price - entry_price) / entry_price * 100.0

        allow = (
            mfe_pct > self.cfg.mfe_runoff_threshold_pct
            and cvd_n >= self.cfg.mfe_runoff_cvd_min
        )

        if allow:
            logger.info(
                "breakeven_mfe_runoff",
                candles=candles_open,
                entry=round(entry_price, 6),
                peak=round(peak_price, 6),
                mfe_pct=round(mfe_pct, 4),
                cvd_n=round(cvd_n, 4),
                hint="BE padrão ignorado",
            )

        return allow

    # ──────────────────────────── BE PADRÃO (v3.6) ────────────────────────

    def check(
        self,
        entry_price:     float,
        sl_price:        float,
        candles_open:    int,
        trailing_active: bool,
        current_close:   float,
    ) -> tuple[float, bool]:
        """
        BE padrão com cap de loss — v3.6, sem alterações na lógica central.

        Chamar APÓS check_mfe_exit() e check_mfe_runoff().
        Se check_mfe_runoff() retornou True, NÃO chamar este método.

        Args:
            entry_price:     preço de entrada da posição.
            sl_price:        SL atual (pode já estar em breakeven).
            candles_open:    candles que a posição está aberta.
            trailing_active: True se trailing stop foi ativado.
            current_close:   preço de fechamento do candle atual.

        Returns:
            (new_sl_price, triggered):
                new_sl_price: entry_price se BE ativou; sl_price original antes do trigger.
                triggered:    True se o trade deve fechar como BREAKEVEN agora.
        """
        # Trailing ativo → BE não interfere
        if trailing_active:
            return sl_price, False

        # Antes do trigger → janela normal
        if candles_open < self.cfg.trigger_candles:
            return sl_price, False

        # A partir do trigger: SL move para entry_price
        new_sl = max(entry_price, sl_price)

        # Nível do cap de loss
        cap_floor = entry_price * (1.0 - self.cfg.loss_cap_pct / 100.0)

        # Trigger 1: cap de loss atingido
        if current_close <= cap_floor:
            logger.info(
                "breakeven_cap_loss",
                candles=candles_open,
                entry=round(entry_price, 6),
                cap_floor=round(cap_floor, 6),
                close=round(current_close, 6),
                loss_pct=round((entry_price - current_close) / entry_price * 100.0, 4),
            )
            return new_sl, True

        # Trigger 2: SL no entry atingido
        if current_close <= new_sl:
            logger.info(
                "breakeven_sl_at_entry",
                candles=candles_open,
                entry=round(entry_price, 6),
                sl=round(new_sl, 6),
                close=round(current_close, 6),
            )
            return new_sl, True

        # BE ativo mas trade ainda aberto (close acima do entry)
        if new_sl > sl_price:
            logger.info(
                "breakeven_activated",
                candles=candles_open,
                entry=round(entry_price, 6),
                old_sl=round(sl_price, 6),
                new_sl=round(new_sl, 6),
                close=round(current_close, 6),
                cap_floor=round(cap_floor, 6),
            )

        return new_sl, False
