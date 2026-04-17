"""
src/execution/execution_engine.py
Execution Engine — orquestra entradas, saídas e monitoramento de posições.

Responsabilidades:
  1. Consumir OrderSpec do RiskManager e executar entrada MARKET
  2. Configurar SL inicial (STOP_MARKET) imediatamente após a entrada
  3. Monitorar posições abertas a cada candle fechado (via PositionMonitor)
  4. Emitir ordens de saída MARKET quando trailing/max_hold/kill_switch disparam
  5. Cancelar o SL antes de qualquer saída não-SL (evitar ordem dupla)
  6. Atualizar portfolio_value via polling periódico da conta
  7. Emitir ExecReport/ClosedExecReport para o StateManager (output_queue)

Modo DRY RUN (config: execution.dry_run = true):
  - Dados de mercado: mainnet real (wss://fstream.binance.com)
  - Ordens: 100% simuladas — nenhuma chamada de ordem à Binance
  - Fills simulados: entry_price do sinal (sem slippage), SL verificado via candle.low
  - Portfolio: inicia em initial_capital_usdt, atualizado com PnL simulado
  - Recovery: injeta todas as posições abertas do DB sem reconciliar com Binance

Fluxo de dados:
  RiskManager.output_queue → [OrderSpec] → _order_loop → _handle_entry()
  DataLayer fanout candle  → [ClosedCandle] → _candle_loop → _handle_close()
  ExecutionEngine.output_queue → [ExecReport | ClosedExecReport] → StateManager

Nota sobre SL real vs. trailing:
  O SL (STOP_MARKET) é colocado na Binance e dispara automaticamente.
  O trailing stop é monitorado por software (no PositionMonitor).
  Quando o trailing dispara, cancelamos o STOP_MARKET e colocamos MARKET sell.
  Em dry_run, o SL é simulado via check_sl_breach() no candle loop.
"""

import asyncio
import os
import time
from typing import Optional

import structlog

from src.data.models              import ClosedCandle
from src.execution.binance_exec_client import BinanceExecClient
from src.execution.models         import ClosedExecReport, ExecReport, FillResult, PositionStateUpdate
from src.execution.position_monitor   import PositionMonitor
from src.risk.models              import CloseInstruction, OrderSpec, Position
from src.risk.risk_manager        import RiskManager

# Import condicional para evitar circular dependency — FeatureEngine é opcional
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.features.feature_engine import FeatureEngine

log = structlog.get_logger(__name__)


class ExecutionEngine:
    """
    Motor de execução paper trading via Binance Futures Testnet.

    Uso:
        engine = ExecutionEngine(config, risk_manager)
        await engine.initialize(universe_symbols)
        await engine.run(order_spec_queue, candle_queue)
    """

    def __init__(self, config: dict, risk_manager: RiskManager, feature_engine: "FeatureEngine | None" = None):
        self._cfg       = config
        self._rm        = risk_manager
        # v3.3: referência ao FeatureEngine para acesso ao cache cvd_n (FlowCB)
        self._feature_engine: "FeatureEngine | None" = feature_engine

        cfg_exec    = config.get("execution", {})
        cfg_risk    = config.get("risk", {})

        # ── Modo dry_run ────────────────────────────────────────────────────
        # Se True: dados reais de mainnet, ordens 100% simuladas localmente.
        # Se False: ordens reais enviadas para Binance (testnet ou produção).
        self._dry_run = cfg_exec.get("dry_run", False)

        # Credenciais via variáveis de ambiente (nunca no config/yaml)
        # Em dry_run, o cliente ainda é criado mas suas chamadas de ordem
        # não são invocadas — apenas fetch_lot_sizes() no initialize().
        if self._dry_run:
            api_key    = ""
            api_secret = ""
        else:
            api_key    = os.environ.get("BINANCE_TESTNET_API_KEY", "")
            api_secret = os.environ.get("BINANCE_TESTNET_API_SECRET", "")

        base_url = config.get("network", {}).get(
            "execution_rest_url", "https://testnet.binancefuture.com"
        )
        self._client  = BinanceExecClient(api_key, api_secret, base_url)
        self._monitor = PositionMonitor(risk_manager)

        # Configuração de execução
        self._leverage    = cfg_exec.get("leverage", cfg_risk.get("leverage", 3))
        self._margin_type = cfg_exec.get("margin_type", "ISOLATED")
        self._balance_poll_interval = cfg_exec.get("balance_poll_interval_sec", 60)

        # Capital inicial (fallback enquanto o poll não roda)
        # Em dry_run: base permanente para simular portfolio_value
        self._initial_capital     = cfg_risk.get("initial_capital_usdt", 10_000.0)
        self._portfolio_value     = self._initial_capital
        self._wallet_balance      = self._initial_capital

        # Mapas por position_id
        self._sl_orders:    dict[str, int]   = {}   # position_id → SL order_id
        self._fill_qty:     dict[str, float] = {}   # position_id → fill qty real
        self._entry_prices: dict[str, float] = {}   # position_id → entry price real

        # Margem alocada em posições abertas (dry_run).
        # Rastreia o capital imobilizado para que available_capital reflita o
        # capital realmente disponível para novas posições — sem isso, o Sizer
        # usa sempre o capital inicial (10k) como base, independente de quantas
        # posições já estão abertas, levando a super-alavancagem ilimitada.
        # Unidade: USDT de margem = notional / leverage.
        self._allocated_margin: float = 0.0

        # Fila de saída para o StateManager
        self.output_queue: asyncio.Queue = asyncio.Queue(maxsize=2_000)

        # Limite de posições abertas simultâneas.
        # Evita abertura ilimitada de posições durante períodos de alta volatilidade.
        # Configurável via execution.max_open_positions (default: 50).
        self._max_positions: int = cfg_exec.get("max_open_positions", 50)

        # Conjunto de símbolos com entrada em andamento (deduplicação de ordens).
        # Necessário porque _order_loop cria tasks em paralelo — múltiplos
        # OrderSpec para o mesmo símbolo chegam antes de qualquer add() completar.
        # Remove quando a task de entrada termina (sucesso ou falha).
        self._pending_symbols: set[str] = set()

        # Contadores
        self._entries_attempted = 0
        self._entries_ok        = 0
        self._entries_failed    = 0
        self._exits_ok          = 0
        self._exits_failed      = 0

        if self._dry_run:
            log.info(
                "execution_engine_dry_run_mode",
                hint="Ordens simuladas localmente — nenhuma ordem real será enviada",
                initial_capital=self._initial_capital,
            )

    # ── Inicialização ─────────────────────────────────────────────────────────

    async def initialize(self, universe_symbols: list[str]) -> None:
        """
        Pré-carrega lot sizes para todos os símbolos do universo.
        Deve ser chamado antes de run().

        Args:
            universe_symbols: lista de símbolos (ex: ["BTCUSDT", "ETHUSDT", ...])
        """
        log.info("execution_engine_initializing", universe_size=len(universe_symbols), dry_run=self._dry_run)

        # Lot sizes sempre necessários (em dry_run para validar qty mínimo;
        # em live para sizing correto na ordem). Usa endpoint público — sem auth.
        await self._client.fetch_lot_sizes(universe_symbols)

        if self._dry_run:
            # Em dry_run não há conta real — portfolio simulado a partir do config
            log.info(
                "execution_engine_ready_dry_run",
                simulated_capital_usdt=round(self._portfolio_value, 2),
                mode="DRY_RUN",
            )
            return

        # Modo live: valida credenciais e lê saldo real
        if not os.environ.get("BINANCE_TESTNET_API_KEY"):
            log.warning(
                "testnet_credentials_missing",
                hint="Defina BINANCE_TESTNET_API_KEY e BINANCE_TESTNET_API_SECRET no .env",
                portfolio_value_fallback=self._portfolio_value,
            )
        else:
            try:
                available, wallet = await self._client.get_account_summary()
                if available > 0 or wallet > 0:
                    self._portfolio_value = available   # sizing: capital livre
                    self._wallet_balance  = wallet      # KS: capital total realizado
                    log.info(
                        "execution_engine_ready",
                        available_balance_usdt=round(available, 2),
                        wallet_balance_usdt=round(wallet, 2),
                        portfolio_value_set=round(self._portfolio_value, 2),
                    )
                else:
                    log.warning(
                        "testnet_balance_zero_or_unavailable",
                        portfolio_value_fallback=self._portfolio_value,
                        hint="Verifique se a conta testnet tem saldo e se as credenciais estão corretas",
                    )
            except Exception as exc:
                log.error(
                    "testnet_balance_fetch_failed",
                    error=str(exc),
                    portfolio_value_fallback=self._portfolio_value,
                    hint="Engine vai usar initial_capital_usdt do config como fallback",
                )

    # ── Loop Principal ────────────────────────────────────────────────────────

    async def run(
        self,
        order_queue:  asyncio.Queue,   # OrderSpec do RiskManager
        candle_queue: asyncio.Queue,   # ClosedCandle (fanout do DataLayer)
    ) -> None:
        """
        Inicia os três loops concorrentes:
          - _order_loop:   consome OrderSpec e executa entradas
          - _candle_loop:  monitora posições a cada candle fechado
          - _balance_loop: atualiza portfolio_value periodicamente

        Encerra limpo quando cancelado (KeyboardInterrupt ou asyncio.cancel).
        """
        log.info("execution_engine_started")

        try:
            await asyncio.gather(
                self._order_loop(order_queue),
                self._candle_loop(candle_queue),
                self._balance_loop(),
                return_exceptions=False,
            )
        except asyncio.CancelledError:
            pass
        finally:
            await self._client.close()
            log.info(
                "execution_engine_stopped",
                entries_ok=self._entries_ok,
                entries_failed=self._entries_failed,
                exits_ok=self._exits_ok,
                exits_failed=self._exits_failed,
                open_positions=self._monitor.count,
            )

    # ── Order Loop ────────────────────────────────────────────────────────────

    async def _order_loop(self, queue: asyncio.Queue) -> None:
        """
        Consome OrderSpec e processa entradas como tasks não-bloqueantes.
        Cada entrada roda em paralelo para não travar o candle loop.

        Deduplicação em dois níveis:
          1. Aqui (antes de criar a task): rejeita se símbolo já está aberto ou
             tem entrada em andamento (_pending_symbols).
          2. Em _handle_entry(): guarda adicional contra race conditions tardias.
        """
        while True:
            try:
                spec: OrderSpec = await queue.get()

                # Nível 1: deduplicação síncrona antes de criar a task.
                # _monitor.symbols = posições já abertas
                # _pending_symbols  = entradas em andamento (tasks criadas mas não concluídas)
                if spec.symbol in self._monitor.symbols or spec.symbol in self._pending_symbols:
                    log.debug(
                        "order_loop_skip_duplicate",
                        symbol=spec.symbol,
                        pid=spec.position_id[:8],
                        already_open=spec.symbol in self._monitor.symbols,
                        pending=spec.symbol in self._pending_symbols,
                    )
                    continue

                # Nível 2: hard cap de posições abertas simultâneas.
                # Conta monitor (abertas) + pending (em andamento) para evitar
                # burst de entradas logo após o limite ser atingido.
                total_open = self._monitor.count + len(self._pending_symbols)
                if total_open >= self._max_positions:
                    log.debug(
                        "order_loop_skip_max_positions",
                        symbol=spec.symbol,
                        open=self._monitor.count,
                        pending=len(self._pending_symbols),
                        limit=self._max_positions,
                    )
                    continue

                self._pending_symbols.add(spec.symbol)
                asyncio.create_task(
                    self._handle_entry(spec),
                    name=f"entry_{spec.symbol}_{spec.position_id[:6]}",
                )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("order_loop_error", error=str(exc))

    # ── Candle Loop ───────────────────────────────────────────────────────────

    async def _candle_loop(self, queue: asyncio.Queue) -> None:
        """
        Processa ClosedCandle:
          1. [Dry run] Verifica SL breach por candle.low (simula STOP_MARKET server-side)
          2. Verifica kill switch (portfolio-level)
          3. Avalia posições do símbolo (trailing + max_hold)
          4. Executa saídas como tasks não-bloqueantes
        """
        while True:
            try:
                candle: ClosedCandle = await queue.get()
                current_close = candle.candle.close

                # 1. [DRY RUN ONLY] Simula SL server-side verificando candle.low
                #    Em live, o STOP_MARKET da Binance dispara automaticamente;
                #    aqui simulamos o mesmo comportamento via candle.low ≤ sl_price.
                #    Deve rodar ANTES do on_candle() para evitar que o trailing
                #    modifique o sl_price de uma posição que já deveria ter fechado.
                if self._dry_run:
                    sl_instructions = await self._monitor.check_sl_breach(candle)
                    for ci in sl_instructions:
                        asyncio.create_task(
                            self._handle_close(ci, ci.close_price or current_close),
                            name=f"sl_sim_{ci.symbol}_{ci.position_id[:6]}",
                        )

                # 2. Kill switch — avalia com portfolio atual
                ks_instructions = await self._monitor.check_kill_switch(
                    self._portfolio_value
                )
                for ci in ks_instructions:
                    asyncio.create_task(
                        self._handle_close(ci, current_close),
                        name=f"ks_close_{ci.symbol}_{ci.position_id[:6]}",
                    )

                # 3. Trailing + MaxHold + DecaySL + FlowCB para o símbolo do candle
                # v3.3: alimenta features do cache do FeatureEngine para o FlowCB
                features_by_symbol: dict | None = None
                if self._feature_engine is not None:
                    fs = self._feature_engine.get_features(candle.symbol)
                    if fs is not None:
                        features_by_symbol = {candle.symbol: {"cvd_n": fs.cvd_n}}

                instructions, updated_positions = await self._monitor.on_candle(
                    candle, features_by_symbol=features_by_symbol
                )
                for ci in instructions:
                    asyncio.create_task(
                        self._handle_close(ci, ci.close_price or current_close),
                        name=f"close_{ci.symbol}_{ci.position_id[:6]}",
                    )

                # 4. Persiste estado incremental (candles_open, peak, sl_price,
                #    trailing_active) das posições que continuam abertas.
                #    Garante que um restart recupere o estado correto do trailing
                #    e do MaxHold — sem isso, candles_open fica sempre 0 no DB.
                for pos in updated_positions:
                    upd = PositionStateUpdate(position=pos)
                    try:
                        self.output_queue.put_nowait(upd)
                    except asyncio.QueueFull:
                        pass   # estado incremental é best-effort — não bloqueia o loop

                # 4. Atualiza portfolio no RiskManager (drawdown tracking)
                self._rm.update_portfolio(self._wallet_balance)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("candle_loop_error", error=str(exc))

    # ── Balance Loop ─────────────────────────────────────────────────────────

    async def _balance_loop(self) -> None:
        """
        Atualiza portfolio_value (available) e wallet_balance a cada intervalo.

        portfolio_value → usado para sizing de novas posições (capital livre)
        wallet_balance  → usado pelo Kill Switch (capital total realizado)

        Em dry_run: não há API real. O portfolio_value é atualizado diretamente
        em _handle_close() (acumulando PnL simulado). Este loop fica no-op.
        """
        if self._dry_run:
            # Dry run: portfolio atualizado via PnL acumulado em _handle_close().
            # Loop mantido por simetria de cancelamento mas sem polling.
            try:
                await asyncio.sleep(float("inf"))
            except asyncio.CancelledError:
                pass
            return

        while True:
            try:
                await asyncio.sleep(self._balance_poll_interval)
                available, wallet = await self._client.get_account_summary()
                if available > 0 or wallet > 0:
                    self._portfolio_value = available
                    self._wallet_balance  = wallet
                    log.debug(
                        "portfolio_updated",
                        balance_usdt=round(available, 2),
                        wallet_balance_usdt=round(wallet, 2),
                    )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("balance_poll_failed", error=str(exc))

    # ── Entrada ───────────────────────────────────────────────────────────────

    async def _handle_entry(self, spec: OrderSpec) -> None:
        """
        Processa um OrderSpec:
          [Live]     1. Setup do símbolo (leverage + margin type)
          [Live]     2. MARKET BUY real na Binance
          [Live]     3. Ajuste do SL ao fill real (slippage)
          [Live]     4. STOP_MARKET SELL para SL
          [Dry run]  1-4. Fill e SL simulados localmente (sem API calls de ordem)
          Ambos:     5. Cria Position e adiciona ao PositionMonitor
                     6. Emite ExecReport para StateManager
        """
        self._entries_attempted += 1
        symbol = spec.symbol

        try:
            if self._dry_run:
                # ── Simulação de fill (sem slippage — preço exato do sinal) ──
                fill = FillResult(
                    order_id        = 0,
                    symbol          = symbol,
                    side            = "BUY",
                    fill_price      = spec.entry_price,
                    fill_qty        = spec.qty,
                    commission_usdt = spec.entry_price * spec.qty * 0.0004,   # 0.04% taker fee
                    ts_ms           = int(time.time() * 1000),
                )
                # Em dry_run, o SL não é colocado na Binance — check_sl_breach()
                # vai simular o disparo via candle.low no _candle_loop().
                actual_sl   = spec.sl_price   # sem ajuste de slippage
                sl_order_id = 0               # sem ordem real

            else:
                # ── Execução real ─────────────────────────────────────────────
                # 1. Setup
                await self._client.setup_symbol(symbol, self._leverage, self._margin_type)

                # 2. Entrada MARKET
                fill = await self._client.place_market_order(symbol, "BUY", spec.qty)

                # 3. Ajusta SL ao fill real (preserva distância ATR independente de slippage)
                #    actual_sl = fill.fill_price - (spec.entry_price - spec.sl_price)
                atr_offset = spec.entry_price - spec.sl_price
                actual_sl  = fill.fill_price - atr_offset

                # 4. SL STOP_MARKET
                sl_order_id = await self._client.place_stop_market(
                    symbol, "SELL", actual_sl, fill.fill_qty, reduce_only=True
                )

            # 5. Cria Position com dados de fill
            position             = Position.from_order_spec(spec, ts_open=fill.ts_ms)
            position.entry_price = fill.fill_price
            position.peak        = fill.fill_price
            position.sl_price    = actual_sl
            position.qty         = fill.fill_qty

            # Registra nos mapas e no monitor
            self._sl_orders[spec.position_id]    = sl_order_id
            self._fill_qty[spec.position_id]     = fill.fill_qty
            self._entry_prices[spec.position_id] = fill.fill_price

            # [Dry run] Registra margem alocada para reduzir available_capital
            if self._dry_run:
                margin_used = (fill.fill_price * fill.fill_qty) / max(self._leverage, 1)
                self._allocated_margin += margin_used

            await self._monitor.add(position)

            # 6. Emite ExecReport para StateManager
            # v3.7: extrai features do FeatureSet via Signal para instrumentação
            fs = spec.signal.feature_set if (spec.signal and spec.signal.feature_set) else None
            report = ExecReport(
                position_id=spec.position_id,
                symbol=symbol,
                entry_fill=fill,
                sl_order_id=sl_order_id,
                leverage=self._leverage,
                sl_price=actual_sl,
                # Metadados do sinal
                tier=spec.tier,
                score=spec.score,
                oi_regime=spec.oi_regime,
                entry_atr=spec.entry_atr,  # v3.3: ATR fixo para DecaySL
                # v3.7: instrumentação — caminho de entrada e features
                entry_path=spec.signal.trigger_type.value.lower() if spec.signal else None,
                entry_zvol=getattr(fs, "zvol_raw", None) if fs else None,
                entry_cvd_z=getattr(fs, "cvd_n", None) if fs else None,
                entry_lsr_z=getattr(fs, "lsr_n", None) if fs else None,
                entry_adx=getattr(fs, "adx14", None) if fs else None,
                entry_kalman_r2=getattr(fs, "kalman_r2", None) if fs else None,
            )
            await self.output_queue.put(report)
            self._entries_ok += 1

            log.info(
                "entry_complete",
                symbol=symbol,
                pid=spec.position_id[:8],
                mode="DRY_RUN" if self._dry_run else "LIVE",
                fill_price=round(fill.fill_price, 4),
                fill_qty=fill.fill_qty,
                sl=round(actual_sl, 4),
                leverage=self._leverage,
            )

        except Exception as exc:
            self._entries_failed += 1
            log.error(
                "entry_failed",
                symbol=symbol,
                pid=spec.position_id[:8],
                error=str(exc),
                entry_price=spec.entry_price,
                qty=spec.qty,
            )
        finally:
            # Libera o símbolo do set de pendentes — independente de sucesso ou falha.
            # Se a entrada falhou, o símbolo pode ser tentado novamente no próximo sinal.
            self._pending_symbols.discard(symbol)

    # ── Saída ─────────────────────────────────────────────────────────────────

    async def _handle_close(
        self, instruction: CloseInstruction, close_price: float
    ) -> None:
        """
        Processa instrução de encerramento de posição:
          [Live]     1. Cancela STOP_MARKET de SL (evita double-fill)
          [Live]     2. MARKET SELL real na Binance
          [Dry run]  1-2. Saída simulada ao close_price (sem API calls de ordem)
          Ambos:     3. Calcula PnL com entry_price real
                     4. Emite ClosedExecReport para StateManager
                     [Dry run] 5. Atualiza portfolio_value simulado
        """
        pid    = instruction.position_id
        symbol = instruction.symbol
        reason = instruction.reason.value

        qty = self._fill_qty.pop(pid, 0.0)
        if qty <= 0:
            log.warning("close_skip_unknown_position", pid=pid[:8], symbol=symbol, reason=reason)
            return

        # Entry price real (armazenado em _handle_entry para PnL correto)
        entry_price = self._entry_prices.pop(pid, close_price)

        try:
            if self._dry_run:
                # ── Simulação de saída ────────────────────────────────────────
                fill = FillResult(
                    order_id        = 0,
                    symbol          = symbol,
                    side            = "SELL",
                    fill_price      = close_price,
                    fill_qty        = qty,
                    commission_usdt = close_price * qty * 0.0004,
                    ts_ms           = int(time.time() * 1000),
                )
                # Remove SL do mapa (sem cancelar — nunca foi colocado)
                self._sl_orders.pop(pid, None)

            else:
                # ── Execução real ─────────────────────────────────────────────
                # 1. Cancela SL (ignora silenciosamente se já foi preenchido)
                sl_oid = self._sl_orders.pop(pid, None)
                if sl_oid:
                    await self._client.cancel_order(symbol, sl_oid)

                # 2. MARKET SELL
                fill = await self._client.place_market_order(symbol, "SELL", qty)

            # 3. PnL com entry_price real — deduz comissão bilateral estimada (v3.6)
            # Fator 0.0008 = 0.04% taker entrada + 0.04% taker saída.
            # Pragmático para dry run: evita armazenar comissão de entrada no Position.
            # Configurável via execution.commission_factor no YAML (default: 0.0008).
            _commission_factor = self._cfg.get("execution", {}).get("commission_factor", 0.0008)
            gross_pnl = (fill.fill_price - entry_price) * fill.fill_qty
            commission = entry_price * fill.fill_qty * _commission_factor
            pnl_usdt  = gross_pnl - commission
            pnl_pct   = (fill.fill_price - entry_price) / entry_price if entry_price > 0 else 0.0

            # 4. ClosedExecReport — v3.7: inclui gross_pnl e commission separados
            report = ClosedExecReport(
                position_id=pid,
                symbol=symbol,
                close_fill=fill,
                close_reason=reason,
                pnl_usdt=round(pnl_usdt, 6),
                pnl_pct=round(pnl_pct, 6),
                gross_pnl=round(gross_pnl, 6),   # v3.7: PnL bruto antes da comissão
                commission=round(commission, 6),  # v3.7: custo bilateral estimado
            )
            await self.output_queue.put(report)
            self._exits_ok += 1

            # 5. [Dry run] Atualiza portfolio simulado com PnL realizado
            if self._dry_run:
                self._portfolio_value = max(0.0, self._portfolio_value + pnl_usdt)
                self._wallet_balance  = self._portfolio_value
                # Libera a margem alocada desta posição
                margin_freed = (entry_price * qty) / max(self._leverage, 1)
                self._allocated_margin = max(0.0, self._allocated_margin - margin_freed)

            sign = "+" if pnl_usdt >= 0 else ""
            log.info(
                "exit_complete",
                symbol=symbol,
                pid=pid[:8],
                mode="DRY_RUN" if self._dry_run else "LIVE",
                reason=reason,
                fill_price=round(fill.fill_price, 4),
                entry_price=round(entry_price, 4),
                gross_pnl=f"{'+' if gross_pnl >= 0 else ''}{round(gross_pnl, 4)}",
                commission=f"-{round(commission, 4)}",
                pnl_usdt=f"{sign}{round(pnl_usdt, 4)}",
                pnl_pct=f"{sign}{round(pnl_pct * 100, 2)}%",
            )

        except Exception as exc:
            self._exits_failed += 1
            log.error("exit_failed", symbol=symbol, pid=pid[:8], reason=reason, error=str(exc))

    # ── Recovery ─────────────────────────────────────────────────────────────

    async def inject_recovered_position(
        self,
        position:    "Position",
        sl_order_id: int | None,
    ) -> None:
        """
        Injeta uma posição recuperada do StateManager no PositionMonitor.

        Chamado durante o startup pelo StateManager.recover_and_inject().
        Restaura os três mapas necessários para que trailing/max_hold/SL-cancel
        funcionem corretamente após um restart:
          - PositionMonitor._positions  → avalia trailing e max_hold
          - ExecutionEngine._sl_orders  → cancela STOP_MARKET antes de saída software
          - ExecutionEngine._fill_qty   → qty real para a ordem de saída MARKET

        Args:
            position:    Position com estado salvo (peak, candles_open, trailing_active)
            sl_order_id: order_id Binance do STOP_MARKET vigente (None se desconhecido)
        """
        await self._monitor.add(position)

        if sl_order_id:
            self._sl_orders[position.position_id] = sl_order_id

        # qty e entry_price reais — necessários para saída e cálculo de PnL
        self._fill_qty[position.position_id]     = position.qty
        self._entry_prices[position.position_id] = position.entry_price

        # [Dry run] Restaura margem alocada para manter available_capital correto
        if self._dry_run:
            margin_used = (position.entry_price * position.qty) / max(self._leverage, 1)
            self._allocated_margin += margin_used

        log.info(
            "position_injected_from_recovery",
            pid=position.position_id[:8],
            symbol=position.symbol,
            entry=round(position.entry_price, 4),
            sl=round(position.sl_price, 4),
            candles_open=position.candles_open,
            trailing_active=position.trailing_active,
            sl_order_id=sl_order_id,
        )

    # ── Interface ─────────────────────────────────────────────────────────────

    @property
    def dry_run(self) -> bool:
        """True se a engine está em modo dry_run (ordens simuladas)."""
        return self._dry_run

    @property
    def portfolio_value(self) -> float:
        """Capital total simulado (initial_capital + PnL acumulado)."""
        return self._portfolio_value

    @property
    def available_capital(self) -> float:
        """
        Capital disponível para sizing de novas posições.

        Em dry_run: portfolio_value menos a margem já alocada em posições abertas.
        Impede que o Sizer use o capital total como base quando há posições abertas —
        corrige a super-alavancagem ilimitada do dry_run original.

        Em live: equivalente a portfolio_value (o saldo availableBalance já reflete
        a margem usada diretamente na conta da Binance).
        """
        if self._dry_run:
            return max(0.0, self._portfolio_value - self._allocated_margin)
        return self._portfolio_value

    @property
    def wallet_balance(self) -> float:
        """Capital total realizado — base para Kill Switch e drawdown tracking."""
        return self._wallet_balance

    @property
    def open_positions(self) -> int:
        """Número de posições abertas no momento."""
        return self._monitor.count

    @property
    def open_symbols(self) -> set[str]:
        """Conjunto de símbolos com posição aberta."""
        return self._monitor.symbols
