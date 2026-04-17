"""
src/state/state_manager.py
StateManager — orquestra persistência e recovery de posições.

Responsabilidades:
  1. Consumir ExecReport/ClosedExecReport da fila do ExecutionEngine
       ExecReport       → persiste posição aberta no SQLite
       ClosedExecReport → fecha posição no SQLite, loga trade
  2. No startup: recuperar posições abertas do SQLite e injetá-las de volta
     no PositionMonitor do ExecutionEngine, restaurando:
       - Position (com peak, candles_open, trailing_active do último candle salvo)
       - _sl_orders[position_id]  → para cancel antes de saída por trailing
       - _fill_qty[position_id]   → para qty real na ordem de saída MARKET
  3. Reconciliação com Binance: posições em DB-OPEN mas ausentes na Binance
     foram fechadas pelo SL server-side enquanto a engine estava down.
     São marcadas como CLOSED com reason='SL_SERVER_SIDE'.

Fluxo de recovery (startup):
  StateManager.recover_and_inject(execution_engine, binance_client)
    ├── db.get_open_positions()            → posições em DB
    ├── binance_client.get_open_positions() → posições reais na Binance
    ├── Reconcilia: DB-OPEN ∩ Binance-OPEN  → injeta no PositionMonitor
    └── DB-OPEN ∖ Binance-OPEN              → marca como CLOSED (SL server-side)

Fluxo de runtime:
  StateManager.run(output_queue)
    ├── ExecReport       → db.insert_open() + position snapshot
    └── ClosedExecReport → db.close_position() + log de trade

Atualização de estado do monitor:
  A cada ClosedExecReport emitido, o ExecutionEngine já removeu a posição
  do PositionMonitor. Para os estados intermediários (peak, candles_open,
  trailing_active), o StateManager recebe atualizações via update_position().
  Isso garante que um restart parcial (ex: crash no meio de um candle)
  recupere estado suficientemente próximo do real.
"""

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import structlog

from src.execution.models  import ClosedExecReport, ExecReport, PositionStateUpdate
from src.risk.models        import Position
from src.state.database     import PositionDB

if TYPE_CHECKING:
    from src.execution.execution_engine    import ExecutionEngine
    from src.execution.binance_exec_client import BinanceExecClient
    from src.monitor.event_bus             import EventBus

log = structlog.get_logger(__name__)


class StateManager:
    """
    Gerencia o ciclo de vida persistido das posições.

    Uso:
        sm = StateManager(config)
        await sm.initialize()

        # Startup: recupera e injeta posições do DB
        recovered = await sm.recover_and_inject(execution_engine, binance_client)

        # Runtime: consome output_queue do ExecutionEngine
        asyncio.create_task(sm.run(execution_engine.output_queue))
    """

    def __init__(self, config: dict, event_bus: Optional["EventBus"] = None):
        cfg_state = config.get("state", {})
        # DB_PATH env var sobrescreve o config — usado pelo Railway para apontar ao Volume
        db_path   = os.environ.get("DB_PATH") or cfg_state.get("db_path", "alpha_river_state.db")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._db  = PositionDB(db_path)

        # Cache de posições abertas: position_id → Position
        # Mantido em memória para permitir update_position() sem re-ler o DB
        self._open: dict[str, Position] = {}

        # EventBus para broadcasting de eventos ao dashboard (opcional)
        self._bus: Optional["EventBus"] = event_bus

        # Modo dry_run: pula reconciliação com Binance no recovery
        self._dry_run: bool = config.get("execution", {}).get("dry_run", False)

    async def initialize(self) -> None:
        """
        Cria o banco e aplica o schema. Chamar uma vez no startup.

        Se a variável de ambiente RESET_DB=1 estiver definida, o arquivo do banco
        é deletado antes de inicializar — inicia do zero com capital limpo.
        Use via Railway Settings > Variables: adicione RESET_DB=1, faça redeploy,
        depois remova a variável para evitar reset em futuros deploys.
        """
        # ── Reset controlado via env var ─────────────────────────────────────────
        if os.environ.get("RESET_DB") == "1":
            db_file = Path(self._db_path)
            if db_file.exists():
                db_file.unlink()
                log.warning(
                    "state_manager_db_reset",
                    db_path=self._db_path,
                    hint="RESET_DB=1 detectado — banco deletado. Remova a variável para evitar reset em futuros deploys.",
                )
            else:
                log.info("state_manager_db_reset_noop", hint="RESET_DB=1 mas banco não existia — nenhuma ação.")

        await self._db.initialize()

        stats = await self._db.get_stats()
        log.info(
            "state_manager_initialized",
            **stats,
        )

    # ── Recovery ──────────────────────────────────────────────────────────────

    async def recover_and_inject(
        self,
        execution_engine: "ExecutionEngine",
        binance_client:   "BinanceExecClient",
    ) -> int:
        """
        Recupera posições abertas do SQLite, reconcilia com a Binance e injeta
        no ExecutionEngine (PositionMonitor + mapas _sl_orders/_fill_qty).

        Returns:
            Número de posições efetivamente injetadas no PositionMonitor.
        """
        # 1. Posições abertas no DB
        db_rows = await self._db.get_open_positions()
        if not db_rows:
            log.info("state_recovery_no_open_positions")
            return 0

        # 2. Posições realmente abertas na Binance (reconciliação)
        #    Dry run: sem ordens reais na Binance — injeta tudo sem reconciliar.
        if self._dry_run:
            binance_symbols = None
            log.info(
                "state_recovery_dry_run_skip_reconciliation",
                hint="Dry run: todas as posições DB-OPEN são injetadas sem reconciliar com Binance",
            )
        else:
            try:
                binance_positions = await binance_client.get_open_positions()
                binance_symbols   = {p["symbol"] for p in binance_positions}
            except Exception as exc:
                log.warning(
                    "state_recovery_binance_unreachable",
                    error=str(exc),
                    hint="Injetando todas as posições do DB sem reconciliar — possível posição fantasma",
                )
                binance_symbols = None

        # 3. Processar cada posição do DB
        injected  = 0
        reconciled = 0

        for row in db_rows:
            pid    = row["position_id"]
            symbol = row["symbol"]

            # 3a. Reconciliação: posição não está mais na Binance → SL disparou
            if binance_symbols is not None and symbol not in binance_symbols:
                await self._db.mark_closed_no_fill(
                    position_id=pid,
                    symbol=symbol,
                    reason="SL_SERVER_SIDE",
                )
                reconciled += 1
                log.info(
                    "state_recovery_sl_server_side",
                    pid=pid[:8],
                    symbol=symbol,
                    hint="SL disparou enquanto engine estava down — posição fechada no DB",
                )
                continue

            # 3b. Reconstrói Position com estado salvo (peak, candles_open, trailing)
            position = Position(
                position_id     = pid,
                symbol          = symbol,
                entry_price     = row["entry_price"],
                qty             = row["qty"],
                sl_price        = row["sl_price"],
                peak            = row["peak"],
                candles_open    = row["candles_open"],
                tier            = row["tier"],
                score           = row["score"],
                oi_regime       = row["oi_regime"],
                opened_at       = row["opened_at"],
                trailing_active = bool(row["trailing_active"]),
                entry_atr       = row.get("entry_atr", 0.0) or 0.0,        # v3.3: ATR fixo
                cvd_neg_streak  = row.get("cvd_neg_streak", 0) or 0,      # v3.3: streak FlowCB
                breakeven_active= bool(row.get("breakeven_active", 0)),    # v3.4: flag breakeven
            )

            # 3c. Injeta no ExecutionEngine (PositionMonitor + _sl_orders + _fill_qty)
            sl_order_id = row.get("sl_order_id")
            await execution_engine.inject_recovered_position(position, sl_order_id)

            # 3d. Mantém cache local
            self._open[pid] = position
            injected += 1

        log.info(
            "state_recovery_complete",
            injected=injected,
            reconciled_sl=reconciled,
            total_db_open=len(db_rows),
        )
        return injected

    # ── Runtime Loop ──────────────────────────────────────────────────────────

    async def run(self, output_queue: asyncio.Queue) -> None:
        """
        Consome ExecReport e ClosedExecReport da fila do ExecutionEngine.

        ExecReport:
          - Persiste posição no SQLite
          - Adiciona ao cache local

        ClosedExecReport:
          - Fecha posição no SQLite
          - Remove do cache local
          - Loga métricas do trade

        Substitui o exec_report_loop placeholder do main.py.
        """
        log.info("state_manager_loop_started")

        while True:
            try:
                report = await output_queue.get()

                if isinstance(report, ExecReport):
                    await self._handle_open(report)

                elif isinstance(report, ClosedExecReport):
                    await self._handle_close(report)

                elif isinstance(report, PositionStateUpdate):
                    # Persiste estado incremental: candles_open, peak, sl_price,
                    # trailing_active. Emitido a cada candle pelo ExecutionEngine
                    # para posições que continuam abertas após evaluate_positions().
                    await self.update_position(report.position)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("state_manager_loop_error", error=str(exc))

        await self._db.close()
        log.info(
            "state_manager_loop_stopped",
            open_positions_in_cache=len(self._open),
        )

    # ── Handlers ──────────────────────────────────────────────────────────────

    async def _handle_open(self, report: ExecReport) -> None:
        """
        Persiste posição aberta com todos os dados reais de fill.

        Usa report.sl_price (SL ajustado ao fill real pelo ExecutionEngine)
        para garantir que um restart recupere o SL correto — não o estimado
        pelo sinal antes da execução.
        """
        position = Position(
            position_id  = report.position_id,
            symbol       = report.symbol,
            entry_price  = report.entry_fill.fill_price,
            qty          = report.entry_fill.fill_qty,
            sl_price     = report.sl_price,               # SL real ajustado ao fill
            peak         = report.entry_fill.fill_price,  # peak inicial = entry
            opened_at    = report.entry_fill.ts_ms,
            # Metadados do sinal — ExecReport passou a carregá-los no fix3
            tier         = report.tier,
            score        = report.score,
            oi_regime    = report.oi_regime,
            entry_atr    = report.entry_atr,  # v3.3: ATR fixo para DecaySL
        )

        await self._db.insert_open(report, position)
        self._open[report.position_id] = position

        # Notifica dashboard
        if self._bus:
            self._bus.publish("position_opened", {
                "position_id":    position.position_id,
                "symbol":         position.symbol,
                "entry_price":    position.entry_price,
                "qty":            position.qty,
                "sl_price":       position.sl_price,
                "peak":           position.peak,
                "candles_open":   0,
                "tier":           position.tier,
                "score":          round(position.score, 4),
                "oi_regime":      position.oi_regime,
                "trailing_active": False,
                "opened_at":      position.opened_at,
            })

        log.info(
            "state_open_persisted",
            pid=report.position_id[:8],
            symbol=report.symbol,
            fill_price=round(report.entry_fill.fill_price, 4),
            fill_qty=report.entry_fill.fill_qty,
            sl_price=round(report.sl_price, 4),
            sl_order_id=report.sl_order_id,
        )

    async def _handle_close(self, report: ClosedExecReport) -> None:
        """
        Fecha posição no DB e loga trade completo.
        """
        await self._db.close_position(report)
        self._open.pop(report.position_id, None)

        # Notifica dashboard
        if self._bus:
            import time as _time
            self._bus.publish("position_closed", {
                "position_id":  report.position_id,
                "symbol":       report.symbol,
                "close_price":  report.close_fill.fill_price,
                "close_reason": report.close_reason,
                "pnl_usdt":     report.pnl_usdt,
                "pnl_pct":      report.pnl_pct,
                "closed_at":    int(_time.time() * 1000),
            })

        sign = "+" if report.pnl_usdt >= 0 else ""
        log.info(
            "state_trade_closed",
            pid=report.position_id[:8],
            symbol=report.symbol,
            reason=report.close_reason,
            pnl_usdt=f"{sign}{round(report.pnl_usdt, 4)}",
            pnl_pct=f"{sign}{round(report.pnl_pct * 100, 2)}%",
            fill_price=round(report.close_fill.fill_price, 4),
        )

    # ── Atualização de estado do monitor ──────────────────────────────────────

    async def update_position(self, position: Position) -> None:
        """
        Persiste o estado atual do monitor para uma posição aberta.

        Chamado pelo PositionMonitor a cada candle que modifica peak,
        sl_price, candles_open ou trailing_active. Garante que um restart
        recupere o estado mais recente do trailing e do contador MaxHold.
        """
        self._open[position.position_id] = position
        await self._db.update_monitor_state(position)

    # ── Interface ─────────────────────────────────────────────────────────────

    @property
    def open_count(self) -> int:
        """Número de posições abertas no cache do StateManager."""
        return len(self._open)
