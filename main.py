"""
main.py — Entry point do Alpha River Engine.

Pipeline completo (5 engines):
  DataLayer → fanout → FeatureEngine → SignalEngine → RiskManager → ExecutionEngine

Modos de operação:
  testnet  (padrão)    : config/engine.yaml, execução no testnet Binance
  dry_run  (mainnet)   : config/mainnet_dryrun.yaml, dados reais, ordens simuladas

Seleção de config:
  python main.py                           → config/engine.yaml
  CONFIG=config/mainnet_dryrun.yaml python main.py  → dry_run com mainnet

Fases de inicialização:
  1. Warm-up histórico (REST) → inicializa calculators de todos os símbolos
  2. Streaming ao vivo (WebSocket) → produz ClosedCandle em tempo real
  3. FeatureEngine processa ClosedCandle → produz FeatureSet
  4. SignalEngine avalia score e gatilhos → emite Signal
  5. RiskManager valida risco e sizing → emite OrderSpec
  6. ExecutionEngine executa ordens → emite ExecReport/ClosedExecReport
  7. [Dashboard] DashboardServer serve UI web em tempo real

Fanout de candles:
  DataLayer.output_queue → [feature_queue, exec_candle_queue]
  + dashboard.update_price(symbol, close) para unrealized PnL

Credenciais:
  Testnet  : BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET
  Dry run  : sem credenciais necessárias (dados mainnet são públicos)
"""

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import List

import structlog
import yaml
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from src.data.data_layer             import DataLayer
from src.features.feature_engine     import FeatureEngine
from src.signals.signal_engine       import SignalEngine
from src.risk.risk_manager           import RiskManager
from src.execution.execution_engine  import ExecutionEngine
from src.execution.models            import ExecReport, ClosedExecReport
from src.state.state_manager         import StateManager
from src.monitor.event_bus           import EventBus
from src.monitor.dashboard           import DashboardServer

# ──────────────────────────── LOGGING ─────────────────────────────────────────

def setup_logging(level: str = "DEBUG") -> None:
    log_level = getattr(logging, level.upper(), logging.DEBUG)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(),
    )

logger = structlog.get_logger(__name__)

# ──────────────────────────── CONFIG ──────────────────────────────────────────

def load_config() -> dict:
    # Permite selecionar config via variável de ambiente CONFIG=caminho/para/arquivo.yaml
    config_path = Path(os.environ.get("CONFIG", "config/engine.yaml"))
    if not config_path.is_absolute():
        config_path = Path(__file__).parent / config_path

    if not config_path.exists():
        print(f"[ERRO] Config não encontrado: {config_path}", file=sys.stderr)
        sys.exit(1)

    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    return config

# ──────────────────────────── FANOUT ──────────────────────────────────────────

async def candle_fanout(
    source:    asyncio.Queue,
    *targets:  asyncio.Queue,
    dashboard: DashboardServer | None = None,
) -> None:
    """
    Distribui ClosedCandle de uma fila de origem para N filas de destino.
    Se dashboard está ativo, atualiza o cache de preços para unrealized PnL.
    """
    while True:
        try:
            candle = await source.get()
            for q in targets:
                await q.put(candle)
            if dashboard is not None:
                dashboard.update_price(candle.candle.symbol, candle.candle.close)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("candle_fanout_error", error=str(exc))

# ──────────────────────────── MAIN ────────────────────────────────────────────

async def main() -> None:
    config = load_config()
    setup_logging(config.get("monitoring", {}).get("log_level", "DEBUG"))

    dry_run = config.get("execution", {}).get("dry_run", False)
    version = "1.2.0-dryrun-dashboard" if dry_run else "1.1.0-state-manager"
    mode    = "DRY_RUN (mainnet data)" if dry_run else "LIVE (testnet)"

    logger.info(
        "alpha_river_engine_starting",
        version=version,
        mode=mode,
    )

    # ── Componentes ──────────────────────────────────────────────────────────
    event_bus       = EventBus()
    data_layer      = DataLayer(config)
    feature_engine  = FeatureEngine(config)
    signal_engine   = SignalEngine(config)
    risk_manager    = RiskManager(config)
    # v3.3: injeta feature_engine no ExecutionEngine para acesso ao cache cvd_n (FlowCB)
    execution_engine = ExecutionEngine(config, risk_manager, feature_engine=feature_engine)
    state_manager   = StateManager(config, event_bus=event_bus)

    # Dashboard (opcional — ativo se dashboard.enabled = true no config)
    dashboard: DashboardServer | None = None
    cfg_dash = config.get("dashboard", {})
    if cfg_dash.get("enabled", True):
        dashboard = DashboardServer(config, state_manager, event_bus)
        dashboard.set_engine(execution_engine)

    # Filas de fanout
    feature_queue     = asyncio.Queue(maxsize=10_000)
    exec_candle_queue = asyncio.Queue(maxsize=10_000)

    tasks: List[asyncio.Task] = []

    try:
        # ── Fase 1: Warm-up ───────────────────────────────────────────────────
        logger.info("phase_1_warmup_starting")
        history = await data_layer.initialize()
        feature_engine.initialize(history)
        logger.info(
            "phase_1_complete",
            symbols_in_history=len(history),
            universe_size=len(data_layer.symbols),
        )

        # ── Fase 2: ExecutionEngine init ──────────────────────────────────────
        logger.info("phase_2_execution_engine_init", dry_run=dry_run)
        await execution_engine.initialize(list(data_layer.symbols))

        # ── Fase 3: StateManager — init DB + recovery ─────────────────────────
        logger.info("phase_3_state_manager_init")
        await state_manager.initialize()

        recovered = await state_manager.recover_and_inject(
            execution_engine, execution_engine._client
        )
        logger.info("phase_3_recovery_complete", positions_recovered=recovered)

        # ── Fase 4: Streaming + pipeline ─────────────────────────────────────
        logger.info("phase_4_streaming_starting")

        # Callback BTC.D → Kill Switch: conecta o BTCDFetcher ao RiskManager.
        # Sem isso, update_btcd() nunca é chamado e o Kill Switch de dominância fica cego.
        async def _on_btcd_update(raw) -> None:
            risk_manager.update_btcd(raw.btc_dominance)

        await data_layer.start(on_btcd_update=_on_btcd_update)

        tasks = [
            asyncio.create_task(
                candle_fanout(
                    data_layer._output_queue,
                    feature_queue,
                    exec_candle_queue,
                    dashboard=dashboard,
                ),
                name="candle_fanout",
            ),
            asyncio.create_task(
                feature_engine.run(feature_queue),
                name="feature_engine",
            ),
            asyncio.create_task(
                signal_engine.run(feature_engine.output_queue),
                name="signal_engine",
            ),
            asyncio.create_task(
                risk_manager.run(
                    signal_engine.output_queue,
                    lambda: execution_engine.available_capital,
                ),
                name="risk_manager",
            ),
            asyncio.create_task(
                execution_engine.run(
                    risk_manager.output_queue,
                    exec_candle_queue,
                ),
                name="execution_engine",
            ),
            asyncio.create_task(
                state_manager.run(execution_engine.output_queue),
                name="state_manager",
            ),
        ]

        # Dashboard como task separada (não bloqueia pipeline se falhar)
        if dashboard is not None:
            tasks.append(
                asyncio.create_task(
                    dashboard.serve(),
                    name="dashboard",
                )
            )
            logger.info(
                "dashboard_active",
                url=f"http://localhost:{cfg_dash.get('port', 8080)}",
            )

        logger.info(
            "pipeline_active",
            tasks=[t.get_name() for t in tasks],
            mode=mode,
        )

        await asyncio.gather(*tasks)

    except KeyboardInterrupt:
        logger.info("shutdown_requested_by_user")

    except RuntimeError as exc:
        logger.error("runtime_error", error=str(exc))
        sys.exit(1)

    finally:
        logger.info("shutting_down")
        for t in tasks:
            if not t.done():
                t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await data_layer.stop()
        logger.info(
            "shutdown_complete",
            signals_emitted=signal_engine._total_signals,
            features_processed=signal_engine._total_processed,
            entries_ok=execution_engine._entries_ok,
            exits_ok=execution_engine._exits_ok,
            open_positions=execution_engine.open_positions,
            state_manager_open=state_manager.open_count,
        )


if __name__ == "__main__":
    asyncio.run(main())
