"""
src/risk/breakeven.py
Breakeven Stop — Alpha River v3.6

Motivação (v3.6 — 2026-04-13):
  Análise do snapshot live (7.142 trades, 11-13/Abr/2026) revelou que:
  - 100% dos trades vencedores via TRAILING fecham em ≤ 4 candles.
  - Trades que ultrapassam o candle 4 sem trailing ativo são zumbis estatísticos:
    avg_loss=-1.12%, P&L total=-1.724 USDT (34.9% dos trades fechados).
  - Segurar posições além de 8 candles é exponencialmente prejudicial — cada
    candle adicional aumenta o loss esperado sem incremento proporcional de WR.
  - O mecanismo v3.4 (trigger c8) fechava trades como "BREAKEVEN" com losses
    reais de -8% a -11%, pois o preço já estava muito abaixo do entry no momento
    do trigger. O SL nunca chegava ao entry antes do fechamento.

  Grid search sobre os dados reais (2.604 trades afetados):
    Cap 0.25% → P&L simulado: +1.417 USDT (muito apertado, risco de falsos disparos)
    Cap 0.50% → P&L simulado: +1.127 USDT (ótimo: ~4.9 candles para atingir cap)
    Cap 0.75% → P&L simulado: +899 USDT
    Cap 1.00% → P&L simulado: +724 USDT
  Cap 0.50% selecionado: alinhado com a janela c4→c8, robusto nos 3 dias testados.

Nova lógica (v3.6):
  Trigger: candle 4 (era 8)
  Ao atingir o trigger (candles_open >= 4, trailing_active=False):
    1. SL é movido para entry_price (proteção zero-loss)
    2. Cap de loss ativado: se current_close <= entry_price × (1 - cap_pct):
       → Fecha IMEDIATAMENTE (BREAKEVEN) — evita loss ilimitado
    3. Se current_close está entre (entry - cap) e entry:
       → SL está no entry, trade continua aguardando recuperação ou trailing
    4. Se current_close > entry:
       → SL protege no entry, trade pode ativar trailing normalmente
  Candles 5-7: cap continua ativo (fecha se close <= entry - cap%)
  Candle 8: MAX_HOLD fecha qualquer posição ainda aberta (independente do estado)

Resultado esperado (simulação sobre 55h de dados reais):
  P&L total: +42 USDT → +1.127 USDT (+2.551%)
  Trades salvos do loss ilimitado: 1.657 de 2.604 (63.6%)

Parâmetros (configuráveis via engine.yaml):
  breakeven_trigger_candles: 4    (era 8)
  breakeven_loss_cap_pct:    0.50 (novo — % do entry_price)
  max_hold_candles:          8    (era 20, controlado em max_hold.py)
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class BreakevenConfig:
    """Configuração do Breakeven Stop. Lida de risk config no engine.yaml."""
    trigger_candles: int   = 4     # v3.6: era 8; melhores trades resolvem em ≤ 4c
    loss_cap_pct:    float = 0.50  # v3.6: novo — % máx de loss abaixo do entry

    @classmethod
    def from_config(cls, risk_cfg: dict) -> "BreakevenConfig":
        return cls(
            trigger_candles=risk_cfg.get("breakeven_trigger_candles", 4),
            loss_cap_pct=risk_cfg.get("breakeven_loss_cap_pct", 0.50),
        )


class BreakevenStop:
    """
    Gerencia o Breakeven Stop para posições abertas.

    Stateless — o estado (sl_price, trailing_active, candles_open) vive no
    objeto Position. Este módulo apenas decide e aplica a mutação.

    Lógica v3.6 (por candle, após trigger):
      - SL é movido para entry_price na primeira chamada após o trigger
      - Cap de loss: se close <= entry × (1 - cap_pct/100) → fecha (BREAKEVEN)
      - Idempotente: uma vez que sl_price >= entry_price, apenas monitora o cap
      - Trailing ativo: breakeven não interfere (gain protegido pelo trailing)

    Uso pelo RiskManager a cada candle fechado (após DecaySL, antes do Trailing):

        be = BreakevenStop(config)
        new_sl, triggered = be.check(entry_price, sl_price, candles_open,
                                     trailing_active, current_close)
        # new_sl:    SL atualizado (entry_price após trigger, sl_price antes)
        # triggered: True se o trade deve fechar como BREAKEVEN agora
    """

    def __init__(self, config: BreakevenConfig) -> None:
        self.cfg = config

    def check(
        self,
        entry_price:     float,
        sl_price:        float,
        candles_open:    int,
        trailing_active: bool,
        current_close:   float,
    ) -> tuple[float, bool]:
        """
        Verifica e aplica o Breakeven Stop com cap de loss.

        Args:
            entry_price:     Preço de entrada da posição.
            sl_price:        SL atual (pode já estar em breakeven de chamada anterior).
            candles_open:    Candles que a posição está aberta (já incrementado pelo MaxHold).
            trailing_active: True se o trailing stop foi ativado.
            current_close:   Preço de fechamento do candle atual.

        Returns:
            (new_sl_price, triggered):
                new_sl_price: entry_price se breakeven ativou (ou já estava ativo);
                              sl_price original caso o trigger ainda não foi atingido.
                triggered:    True se o trade deve fechar como BREAKEVEN agora.
                              Dispara em dois casos após o trigger:
                              1. close <= entry_price (SL no entry atingido)
                              2. close <= entry_price × (1 - cap_pct/100) (cap de loss)
        """
        # Trailing ativo → breakeven não interfere (gain já protegido)
        if trailing_active:
            return sl_price, False

        # Antes do trigger → janela normal de resolução
        if candles_open < self.cfg.trigger_candles:
            return sl_price, False

        # ── A partir do candle trigger: SL move para entry_price ───────────────
        # O SL fica em max(entry_price, sl_price_atual) para não recuar um
        # trailing que já tenha subido o SL acima do entry.
        new_sl = max(entry_price, sl_price)

        # Nível do cap de loss: preço mínimo tolerado antes de fechar
        cap_floor = entry_price * (1.0 - self.cfg.loss_cap_pct / 100.0)

        # Trigger 1: cap de loss — close caiu além do limite tolerado
        if current_close <= cap_floor:
            logger.info(
                "Breakeven CAP_LOSS | candles=%d entry=%.6f cap_floor=%.6f "
                "close=%.6f loss_pct=%.4f%%",
                candles_open, entry_price, cap_floor,
                current_close,
                (entry_price - current_close) / entry_price * 100,
            )
            return new_sl, True

        # Trigger 2: SL no entry atingido — close voltou para/abaixo do entry
        if current_close <= new_sl:
            logger.info(
                "Breakeven SL_AT_ENTRY | candles=%d entry=%.6f sl=%.6f close=%.6f",
                candles_open, entry_price, new_sl, current_close,
            )
            return new_sl, True

        # Breakeven ativo mas trade ainda aberto (close entre cap_floor e new_sl não existe;
        # close está acima do entry) → SL permanece no entry, trade continua
        if new_sl > sl_price:
            logger.info(
                "Breakeven ATIVADO | candles=%d entry=%.6f old_sl=%.6f "
                "new_sl=%.6f close=%.6f cap_floor=%.6f",
                candles_open, entry_price, sl_price, new_sl,
                current_close, cap_floor,
            )

        return new_sl, False
