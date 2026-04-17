"""
src/risk/risk_manager.py
Orquestrador do Risk Manager — recebe Signal, emite OrderSpec.

Responsabilidades:
  1. Verificar Kill Switch antes de qualquer nova entrada
  2. Verificar Symbol Blacklist (v3.3)
  3. Verificar filtro D2 ATR (v3.3): ATR/entry > 30% → rejeitar
  4. Calcular position size (Sizer)
  5. Calcular SL inicial (StopLossCalculator)
  6. Montar e retornar OrderSpec (com entry_atr — v3.3)
  7. Monitorar posições abertas a cada candle fechado:
       - Verificar SL Time-Decay (v3.3): phases 1/2/3 com mult crescente
       - Verificar Breakeven Stop (v3.6): c4 sem trailing → SL = entry + cap 0.50%
       - Atualizar Flow Circuit Breaker streak (v3.3, desativado em v3.5)
       - Atualizar trailing stop (TrailingStop)
       - Verificar MaxHold (MaxHoldChecker — MH=8 em v3.6)
       - Emitir CloseInstruction se necessário

Ordem de precedência nas verificações de saída (v3.6):
  1. SL Time-Decay  (DECAY_SL)  — SL aperta progressivamente (fases p4/p16)
  2. Breakeven Stop (BREAKEVEN) — c4 sem trailing → SL=entry + cap 0.50% do entry
  3. [Flow CB DESATIVADO em v3.5] — absorvido pelo Breakeven
  4. Trailing Stop  (TRAILING / SL)
  5. MaxHold        (MAX_HOLD)  — c8: saída de último recurso (era c20 em v3.3)

Changelog:
  v3.3 (2026-04-04): SL Time-Decay, Blacklist, D2 ATR Filter, Flow CB
  v3.4 (2026-04-09): Breakeven Stop (c8) — SL move para entry no candle 8
  v3.5 (2026-04-10): trailing_activation 1.003→1.002, trailing_capture 0.80→0.90
                     Flow CB desativado: grid search comprovou absorção pelo Breakeven
                     PF esperado: 0.837→0.870 (grid Jan-Fev 2026, 25 combos)
  v3.6 (2026-04-13): Breakeven trigger c8→c4, cap de loss 0.50% do entry_price
                     max_hold_candles 20→8 (evidência live: decay exponencial pós-c4)
                     Simulação sobre 7.142 trades reais: P&L +42→+1.127 USDT (+2.551%)

Interface pública:
  process_signal(signal, portfolio_value) → Optional[OrderSpec]
  evaluate_positions(positions, closes, features_by_symbol) → List[CloseInstruction]
  update_btcd(btcd_value)                 → None
  update_portfolio(portfolio_value)       → None
  run(input_queue, portfolio_fn)          → async loop
"""

import asyncio
from typing import Callable, Dict, List, Optional

import structlog

from src.signals.models   import Signal
from src.risk.models      import CloseInstruction, CloseReason, OrderSpec, Position
from src.risk.sizer       import Sizer
from src.risk.stop_loss   import StopLossCalculator
from src.risk.trailing    import TrailingStop
from src.risk.max_hold    import MaxHoldChecker
from src.risk.kill_switch import KillSwitch
from src.risk.decay_sl    import DecaySLChecker, DecaySLConfig
from src.risk.flow_cb     import FlowCircuitBreaker, FlowCBConfig
from src.risk.blacklist   import SymbolBlacklist
from src.risk.breakeven   import BreakevenStop, BreakevenConfig

logger = structlog.get_logger(__name__)


class RiskManager:
    """
    Controla todo o ciclo de risco da engine — entradas e saídas.

    v3.3: integra SL Time-Decay, Flow Circuit Breaker, Symbol Blacklist
          e filtro D2 ATR além dos mecanismos originais.

    Uso:
        risk_mgr = RiskManager(config)

        # Nova entrada
        order_spec = risk_mgr.process_signal(signal, portfolio_value=10_000.0)

        # Monitorar posições a cada candle fechado
        closes   = {'BTCUSDT': 51200.0, 'ETHUSDT': 3050.0}
        features = {'BTCUSDT': {'cvd_n': -0.5}, ...}
        close_instructions = risk_mgr.evaluate_positions(open_positions, closes, features)

        # Alimentar BTC.D (chamado pelo BTCDFetcher)
        risk_mgr.update_btcd(54.3)
    """

    def __init__(self, config: dict):
        cfg_risk = config.get("risk", {})
        cfg_ks   = config.get("kill_switch", {})

        # ── Módulos originais ──────────────────────────────────────────────────
        self._sizer = Sizer(
            risk_per_trade_pct=cfg_risk.get("risk_per_trade_pct", 0.01),
            delev_multiplier=cfg_risk.get("delev_size_multiplier", 0.5),
        )
        self._sl_calc = StopLossCalculator(
            atr_multiplier=cfg_risk.get("sl_atr_multiplier", 2.0),
        )
        self._trailing = TrailingStop(
            activation_ratio=cfg_risk.get("trailing_activation_ratio", 1.002),
            capture_ratio=cfg_risk.get("trailing_capture_ratio", 0.90),
        )
        self._max_hold = MaxHoldChecker(
            max_hold_candles=cfg_risk.get("max_hold_candles", 8),
        )
        self._kill_switch = KillSwitch(
            btcd_drop_pct=cfg_ks.get("btcd_drop_pct", 3.0),
            btcd_lookback_hours=cfg_ks.get("btcd_lookback_hours", 4.0),
            portfolio_drawdown_pct=cfg_ks.get("portfolio_drawdown_pct", 0.10),
            cooldown_hours=cfg_ks.get("cooldown_hours", 4.0),
        )

        # ── Módulos v3.3 ──────────────────────────────────────────────────────
        self._decay_sl = DecaySLChecker(
            config=DecaySLConfig.from_config(cfg_risk),
        )
        # v3.5: flow_cb_enabled=false — Breakeven c8 absorve o papel do CB.
        # Grid search Jan-Fev 2026 (85 combos): CB disparou em 0.08% dos trades.
        self._flow_cb_enabled: bool = cfg_risk.get("flow_cb_enabled", False)
        self._flow_cb = FlowCircuitBreaker(
            config=FlowCBConfig.from_config(cfg_risk),
        )
        self._blacklist = SymbolBlacklist.from_config(cfg_risk)

        self._breakeven = BreakevenStop(
            config=BreakevenConfig.from_config(cfg_risk),
        )

        # D2 ATR Filter: ATR/entry_price > threshold → rejeitar entrada
        self._d2_atr_max_ratio: float = cfg_risk.get("d2_atr_entry_max_ratio", 0.30)

        # Fila de saída para o ExecutionEngine
        self._output_queue: asyncio.Queue = asyncio.Queue(maxsize=2_000)

        # Contadores
        self._signals_received    = 0
        self._orders_generated    = 0
        self._signals_blocked     = 0
        self._signals_blacklisted = 0
        self._signals_d2_filtered = 0

    # ──────────────────────────── NOVA ENTRADA ─────────────────────────────

    def process_signal(
        self,
        signal:          Signal,
        portfolio_value: float,
    ) -> Optional[OrderSpec]:
        """
        Converte um Signal em OrderSpec após todas as verificações de risco.

        Bloqueia a entrada se (ordem de verificação):
          1. Kill Switch ativo
          2. Símbolo na blacklist (v3.3)
          3. ATR/entry > d2_atr_max_ratio (D2 ATR Filter, v3.3)
          4. position size seria zero (portfolio_value muito baixo)

        Args:
            signal:          Signal emitido pelo SignalEngine
            portfolio_value: valor atual do portfólio em USDT

        Returns:
            OrderSpec pronto para o ExecutionEngine, ou None se bloqueado.
        """
        self._signals_received += 1

        # 1. Kill Switch
        if self._kill_switch.is_active():
            self._signals_blocked += 1
            logger.debug(
                "signal_blocked_kill_switch",
                symbol=signal.symbol,
                reason=self._kill_switch.reason,
            )
            return None

        # 2. v3.3 — Blacklist
        if self._blacklist.is_blocked(signal.symbol):
            self._signals_blacklisted += 1
            logger.debug("signal_blocked_blacklist", symbol=signal.symbol)
            return None

        # 3. v3.3 — D2 ATR Filter (alta volatilidade → risco assimétrico)
        if signal.entry_price > 0 and signal.atr14 > 0:
            atr_ratio = signal.atr14 / signal.entry_price
            if atr_ratio > self._d2_atr_max_ratio:
                self._signals_d2_filtered += 1
                logger.info(
                    "signal_blocked_d2_atr",
                    symbol=signal.symbol,
                    atr_ratio=round(atr_ratio, 4),
                    threshold=self._d2_atr_max_ratio,
                    atr14=round(signal.atr14, 6),
                    entry=round(signal.entry_price, 4),
                )
                return None

        # 4. Position sizing
        qty, position_usdt = self._sizer.compute(
            portfolio_value=portfolio_value,
            entry_price=signal.entry_price,
            oi_regime=signal.oi_regime,
        )

        if qty <= 0:
            logger.warning(
                "signal_blocked_zero_qty",
                symbol=signal.symbol,
                portfolio_value=portfolio_value,
                entry_price=signal.entry_price,
            )
            return None

        # 5. Stop Loss inicial
        sl_price = self._sl_calc.compute(
            entry_price=signal.entry_price,
            atr14=signal.atr14,
        )

        # 6. Monta OrderSpec (com entry_atr para o SL Decay — v3.3)
        spec = OrderSpec(
            symbol=signal.symbol,
            side="BUY",
            qty=qty,
            entry_price=signal.entry_price,
            sl_price=sl_price,
            entry_atr=signal.atr14,          # v3.3: ATR fixo para DecaySL
            max_hold_candles=self._max_hold._max,
            tier=signal.tier,
            score=signal.score,
            oi_regime=signal.oi_regime,
            signal=signal,
        )

        self._orders_generated += 1

        logger.info(
            "order_spec_created",
            symbol=spec.symbol,
            qty=round(spec.qty, 6),
            entry=round(spec.entry_price, 4),
            sl=round(spec.sl_price, 4),
            entry_atr=round(spec.entry_atr, 6),
            sl_dist_pct=round((spec.entry_price - spec.sl_price) / spec.entry_price * 100, 2),
            position_usdt=round(position_usdt, 2),
            portfolio_value=round(portfolio_value, 2),
            tier=spec.tier,
            score=round(spec.score, 4),
            oi_regime=spec.oi_regime,
            position_id=spec.position_id[:8],
        )

        return spec

    # ──────────────────────────── MONITORAMENTO ────────────────────────────

    def evaluate_positions(
        self,
        positions:          List[Position],
        closes:             Dict[str, float],
        features_by_symbol: Optional[Dict[str, dict]] = None,
    ) -> List[CloseInstruction]:
        """
        Avalia todas as posições abertas a cada candle fechado.

        v3.6 — Ordem de verificação (precedência):
          1. SL Time-Decay (DECAY_SL)   — SL aperta em 3 fases por ATR fixo
          2. Breakeven Stop (BREAKEVEN) — c4 sem trailing: SL→entry + cap 0.50%
          3. [Flow CB DESATIVADO v3.5]  — absorvido pelo Breakeven
          4. Trailing/SL (TRAILING/SL) — trailing gain-based (activation +0.2%)
          5. MaxHold (MAX_HOLD)        — c8: fecha qualquer posição sobrevivente

        IMPORTANTE: MaxHold.check() incrementa candles_open (in-place) e deve
        ser o ÚLTIMO a rodar. As verificações 1-4 usam o candles_open do candle
        ANTERIOR (já incrementado pela iteração anterior). Isso é intencional:
        o trigger do Breakeven em candles_open=4 significa "4 candles já fechados
        sem trailing" — o incremento para 5 ocorre ao final desta mesma iteração.

        Args:
            positions:          lista de posições abertas (modificadas in-place)
            closes:             dict {symbol: current_close_price}
            features_by_symbol: dict {symbol: {'cvd_n': float, ...}} — v3.3 FlowCB.
                                Se None ou símbolo ausente, FlowCB é ignorado.

        Returns:
            Lista de CloseInstruction para posições que devem ser encerradas.
        """
        instructions: List[CloseInstruction] = []
        features_by_symbol = features_by_symbol or {}

        for pos in positions:
            current_close = closes.get(pos.symbol)
            if current_close is None:
                continue  # sem dado de preço para este símbolo — pula

            # ── 1. SL Time-Decay (v3.3) ───────────────────────────────────────
            if pos.entry_atr > 0:
                effective_sl, decay_triggered = self._decay_sl.check(
                    entry_price=pos.entry_price,
                    entry_atr=pos.entry_atr,
                    candles_open=pos.candles_open,
                    current_close=current_close,
                    current_sl_price=pos.sl_price,
                )
                # Atualiza o SL da posição se o decay é mais restritivo
                if effective_sl > pos.sl_price:
                    pos.sl_price = effective_sl

                if decay_triggered:
                    instructions.append(CloseInstruction(
                        position_id=pos.position_id,
                        symbol=pos.symbol,
                        reason=CloseReason.DECAY_SL,
                        close_price=current_close,
                    ))
                    logger.info(
                        "position_close_decay_sl",
                        symbol=pos.symbol,
                        candles_open=pos.candles_open,
                        entry=pos.entry_price,
                        atr=pos.entry_atr,
                        sl=round(pos.sl_price, 4),
                        close=current_close,
                        pnl_pct=round((current_close - pos.entry_price) / pos.entry_price * 100, 2),
                    )
                    continue  # não verifica demais se vai fechar

            # ── 2. Breakeven Stop (v3.6) ──────────────────────────────────────
            # A partir do candle 4 sem trailing ativo:
            #   - SL move para entry_price (proteção zero-loss)
            #   - Cap de loss 0.50%: se close <= entry × 0.995 → fecha (BREAKEVEN)
            #   - Se close > entry: SL protege no entry, trade continua
            # Flag breakeven_active sinaliza que o SL já está no entry (idempotente).
            new_be_sl, be_triggered = self._breakeven.check(
                entry_price=pos.entry_price,
                sl_price=pos.sl_price,
                candles_open=pos.candles_open,
                trailing_active=pos.trailing_active,
                current_close=current_close,
            )
            # Atualiza SL se breakeven moveu para entry (e marca flag idempotente)
            if new_be_sl > pos.sl_price:
                pos.sl_price = new_be_sl
                if not pos.breakeven_active:
                    pos.breakeven_active = True

            if be_triggered:
                # close_price: o real preço de saída (pode ser o cap_floor se
                # o cap de loss disparou, ou current_close se SL no entry foi tocado)
                be_close = current_close
                instructions.append(CloseInstruction(
                    position_id=pos.position_id,
                    symbol=pos.symbol,
                    reason=CloseReason.BREAKEVEN,
                    close_price=be_close,
                ))
                logger.info(
                    "position_close_breakeven",
                    symbol=pos.symbol,
                    candles_open=pos.candles_open,
                    entry=pos.entry_price,
                    close=be_close,
                    sl_at_entry=round(pos.sl_price, 6),
                    cap_pct=self._breakeven.cfg.loss_cap_pct,
                    pnl_pct=round((be_close - pos.entry_price) / pos.entry_price * 100, 4),
                )
                continue

            # ── 3. Flow Circuit Breaker (v3.3 — DESATIVADO em v3.5) ──────────
            # Evidência empírica: com Breakeven c8 ativo, o CB disparou em apenas
            # 0.08% dos 77k trades do backtest Jan-Fev 2026 (85 combos testados).
            # O Breakeven absorve os trades deteriorados antes que o streak CVD
            # negativo se forme. Manter ativo piora avg_loss sem benefício.
            sym_features = features_by_symbol.get(pos.symbol, {})
            cvd_n = sym_features.get("cvd_n")

            if self._flow_cb_enabled and cvd_n is not None:
                new_streak, flow_triggered = self._flow_cb.check(
                    cvd_n=cvd_n,
                    close_price=current_close,
                    entry_price=pos.entry_price,
                    current_streak=pos.cvd_neg_streak,
                )
                pos.cvd_neg_streak = new_streak  # sempre atualiza streak

                if flow_triggered:
                    instructions.append(CloseInstruction(
                        position_id=pos.position_id,
                        symbol=pos.symbol,
                        reason=CloseReason.FLOW_CB,
                        close_price=current_close,
                    ))
                    logger.info(
                        "position_close_flow_cb",
                        symbol=pos.symbol,
                        cvd_n=round(cvd_n, 3),
                        streak=new_streak,
                        entry=pos.entry_price,
                        close=current_close,
                        pnl_pct=round((current_close - pos.entry_price) / pos.entry_price * 100, 2),
                    )
                    continue  # não verifica trailing/MaxHold
            # v3.6: com flow_cb_enabled=False, não há necessidade de resetar
            # cvd_neg_streak — o campo permanece 0 desde a abertura (default).
            # O reset anterior gerava writes desnecessários no DB a cada candle.

            # ── 4. Trailing Stop (SL / TRAILING) ──────────────────────────────
            trail_reason = self._trailing.update(pos, current_close)
            if trail_reason:
                instructions.append(CloseInstruction(
                    position_id=pos.position_id,
                    symbol=pos.symbol,
                    reason=trail_reason,
                    close_price=current_close,
                ))
                logger.info(
                    "position_close_stop",
                    symbol=pos.symbol,
                    reason=trail_reason.value,
                    entry=pos.entry_price,
                    close=current_close,
                    sl=round(pos.sl_price, 4),
                    trailing_active=pos.trailing_active,
                    pnl_pct=round((current_close - pos.entry_price) / pos.entry_price * 100, 2),
                )
                continue  # não verifica MaxHold se já vai fechar

            # ── 5. MaxHold — saída de último recurso ──────────────────────────
            max_hold_reason = self._max_hold.check(pos)
            if max_hold_reason:
                instructions.append(CloseInstruction(
                    position_id=pos.position_id,
                    symbol=pos.symbol,
                    reason=max_hold_reason,
                    close_price=current_close,
                ))
                logger.info(
                    "position_close_max_hold",
                    symbol=pos.symbol,
                    candles_open=pos.candles_open,
                    entry=pos.entry_price,
                    close=current_close,
                    pnl_pct=round((current_close - pos.entry_price) / pos.entry_price * 100, 2),
                )

        return instructions

    def evaluate_kill_switch(
        self,
        positions: List[Position],
        portfolio_value: float,
    ) -> List[CloseInstruction]:
        """
        Avalia o Kill Switch e emite instruções de fechamento para TODAS
        as posições abertas se ativado.

        Returns:
            Lista de CloseInstruction (vazia se Kill Switch não ativado).
        """
        just_activated = self._kill_switch.evaluate(portfolio_value)
        if not just_activated:
            return []

        instructions = [
            CloseInstruction(
                position_id=pos.position_id,
                symbol=pos.symbol,
                reason=CloseReason.KILL_SWITCH,
            )
            for pos in positions
        ]

        logger.warning(
            "kill_switch_closing_positions",
            count=len(instructions),
            reason=self._kill_switch.reason,
        )
        return instructions

    # ──────────────────────────── ALIMENTAÇÃO ──────────────────────────────

    def update_btcd(self, btcd_value: float) -> None:
        """Alimenta novo valor de BTC.D para o Kill Switch."""
        self._kill_switch.update_btcd(btcd_value)

    def update_portfolio(self, portfolio_value: float) -> None:
        """Alimenta valor atual do portfólio (para tracking de peak/drawdown)."""
        self._kill_switch.update_portfolio(portfolio_value)

    # ──────────────────────────── ASYNC LOOP ───────────────────────────────

    async def run(
        self,
        input_queue:   asyncio.Queue,
        portfolio_fn:  Callable[[], float],
    ) -> None:
        """
        Loop assíncrono: consome Signals da fila do SignalEngine,
        processa e coloca OrderSpec na output_queue.

        Args:
            input_queue:  fila de Signal (signal_engine.output_queue)
            portfolio_fn: callable que retorna o valor atual do portfólio
        """
        logger.info("risk_manager_loop_started")

        while True:
            try:
                signal = await input_queue.get()
                order_spec = self.process_signal(signal, portfolio_fn())
                if order_spec is not None:
                    await self._output_queue.put(order_spec)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("risk_manager_error", error=str(exc))

        logger.info(
            "risk_manager_loop_stopped",
            signals_received=self._signals_received,
            orders_generated=self._orders_generated,
            signals_blocked=self._signals_blocked,
            signals_blacklisted=self._signals_blacklisted,
            signals_d2_filtered=self._signals_d2_filtered,
        )

    # ──────────────────────────── INTERFACE ────────────────────────────────

    @property
    def output_queue(self) -> asyncio.Queue:
        """Fila de OrderSpec para o ExecutionEngine."""
        return self._output_queue

    @property
    def kill_switch_active(self) -> bool:
        """True se o Kill Switch está ativo (cooldown em andamento)."""
        return self._kill_switch.is_active()

    @property
    def blacklist(self) -> SymbolBlacklist:
        """Acesso à blacklist para inspeção/debug."""
        return self._blacklist
