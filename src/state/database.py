"""
src/state/database.py
SQLite handler assíncrono para o StateManager.

Responsabilidades:
  - Criar e migrar o schema (CREATE TABLE IF NOT EXISTS — idempotente)
  - Inserir posição aberta ao receber ExecReport
  - Fechar posição ao receber ClosedExecReport
  - Retornar posições abertas para recovery no startup

Schema:
  Tabela `positions`: ciclo de vida completo de cada posição.
    status: 'OPEN' | 'CLOSED'
    sl_order_id: order_id Binance do STOP_MARKET (necessário para cancel na saída)
    trailing_active, peak, candles_open: estado do monitor (atualizado pelo StateManager)

Persistência do arquivo:
  Padrão: alpha_river_state.db no diretório raiz da engine.
  Configurável via config['state']['db_path'].
"""

import time
from pathlib import Path
from typing import Optional

import aiosqlite
import structlog

from src.execution.models import ClosedExecReport, ExecReport
from src.risk.models       import Position

log = structlog.get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    position_id      TEXT PRIMARY KEY,
    symbol           TEXT    NOT NULL,
    entry_price      REAL    NOT NULL,
    qty              REAL    NOT NULL,
    sl_price         REAL    NOT NULL,
    peak             REAL    NOT NULL,
    candles_open     INTEGER NOT NULL DEFAULT 0,
    tier             INTEGER NOT NULL DEFAULT 1,
    score            REAL    NOT NULL DEFAULT 0.0,
    oi_regime        TEXT    NOT NULL DEFAULT 'NEUTRO',
    opened_at        INTEGER NOT NULL,
    trailing_active  INTEGER NOT NULL DEFAULT 0,
    status           TEXT    NOT NULL DEFAULT 'OPEN',
    sl_order_id      INTEGER,
    leverage         INTEGER NOT NULL DEFAULT 3,
    close_reason     TEXT,
    close_price      REAL,
    pnl_usdt         REAL,
    pnl_pct          REAL,
    closed_at        INTEGER,
    entry_atr        REAL    NOT NULL DEFAULT 0.0,
    cvd_neg_streak   INTEGER NOT NULL DEFAULT 0,
    breakeven_active INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol);
"""

# Migrations idempotentes — adicionam colunas a bancos já existentes.
# ALTER TABLE ADD COLUMN falha silenciosamente se a coluna já existe (ignorado via try/except).
_MIGRATIONS_V33 = [
    "ALTER TABLE positions ADD COLUMN entry_atr      REAL    NOT NULL DEFAULT 0.0",
    "ALTER TABLE positions ADD COLUMN cvd_neg_streak INTEGER NOT NULL DEFAULT 0",
]

_MIGRATIONS_V34 = [
    "ALTER TABLE positions ADD COLUMN breakeven_active INTEGER NOT NULL DEFAULT 0",
]

# v3.7: instrumentação de diagnóstico (P65 — AF Parte XXVII)
# Permite separar edge de entrada de custo de transação, rastrear trigger path,
# calcular MFE (Max Favorable Excursion) e correlacionar features vs PnL.
_MIGRATIONS_V37 = [
    "ALTER TABLE positions ADD COLUMN gross_pnl       REAL    DEFAULT NULL",
    "ALTER TABLE positions ADD COLUMN commission      REAL    DEFAULT NULL",
    "ALTER TABLE positions ADD COLUMN entry_path      TEXT    DEFAULT NULL",
    "ALTER TABLE positions ADD COLUMN peak_price      REAL    DEFAULT NULL",
    "ALTER TABLE positions ADD COLUMN entry_zvol      REAL    DEFAULT NULL",
    "ALTER TABLE positions ADD COLUMN entry_cvd_z     REAL    DEFAULT NULL",
    "ALTER TABLE positions ADD COLUMN entry_lsr_z     REAL    DEFAULT NULL",
    "ALTER TABLE positions ADD COLUMN entry_adx       REAL    DEFAULT NULL",
    "ALTER TABLE positions ADD COLUMN entry_kalman_r2 REAL    DEFAULT NULL",
]


class PositionDB:
    """
    Interface assíncrona para persistência de posições em SQLite.

    Uso:
        db = PositionDB(db_path)
        await db.initialize()           # cria schema se não existir
        await db.insert_open(report)    # ao abrir posição
        await db.close_position(report) # ao fechar posição
        positions = await db.get_open_positions()  # no startup
    """

    def __init__(self, db_path: str = "alpha_river_state.db"):
        self._db_path = str(db_path)
        # Conexão persistente — aberta em initialize(), fechada em close().
        # Evita o overhead de abrir/fechar conexão TCP+arquivo em cada operação
        # (cada write anterior fazia ~2 syscalls extras de open/close/fsync).
        self._conn: Optional[aiosqlite.Connection] = None

    async def initialize(self) -> None:
        """
        Abre a conexão persistente e aplica o schema (idempotente).
        Também aplica migrations v3.3 (entry_atr, cvd_neg_streak) em bancos legados.
        Chamar uma vez no startup antes de qualquer operação.
        """
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()

        # Migrations: adiciona colunas em bancos legados (idempotente)
        for stmt in [*_MIGRATIONS_V33, *_MIGRATIONS_V34, *_MIGRATIONS_V37]:
            try:
                await self._conn.execute(stmt)
                await self._conn.commit()
            except Exception:
                pass  # coluna já existe — ignorar

        log.info("state_db_initialized", path=self._db_path)

    async def close(self) -> None:
        """Fecha a conexão persistente. Chamar no shutdown do StateManager."""
        if self._conn:
            await self._conn.close()
            self._conn = None
            log.info("state_db_closed")

    # ── Escrita ───────────────────────────────────────────────────────────────

    async def insert_open(self, report: ExecReport, position: Position) -> None:
        """
        Persiste uma posição recém-aberta.

        Chamado imediatamente após o ExecReport ser emitido pelo ExecutionEngine.
        Salva sl_order_id para que, após um restart, a engine possa cancelar o
        STOP_MARKET antes de emitir a saída via trailing/max_hold.

        Args:
            report:   ExecReport com fill real (price, qty, sl_order_id)
            position: Position criada pelo ExecutionEngine com dados reais de fill
        """
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO positions
                (position_id, symbol, entry_price, qty, sl_price, peak,
                 candles_open, tier, score, oi_regime, opened_at,
                 trailing_active, status, sl_order_id, leverage,
                 entry_atr, cvd_neg_streak, breakeven_active,
                 peak_price,
                 entry_path, entry_zvol, entry_cvd_z, entry_lsr_z,
                 entry_adx, entry_kalman_r2)
            VALUES
                (?, ?, ?, ?, ?, ?,
                 ?, ?, ?, ?, ?,
                 ?, 'OPEN', ?, ?,
                 ?, ?, ?,
                 ?,
                 ?, ?, ?, ?, ?, ?)
            """,
            (
                position.position_id,
                position.symbol,
                position.entry_price,
                position.qty,
                position.sl_price,
                position.peak,
                position.candles_open,
                position.tier,
                position.score,
                position.oi_regime,
                position.opened_at,
                int(position.trailing_active),
                report.sl_order_id,
                report.leverage,
                position.entry_atr,             # v3.3: ATR fixo na entrada
                position.cvd_neg_streak,        # v3.3: streak CVD negativo (0 na abertura)
                int(position.breakeven_active),  # v3.4: flag breakeven (0 na abertura)
                # v3.7: instrumentação — peak_price inicializa igual a entry_price
                position.entry_price,
                # v3.7: features de entrada e trigger path
                report.entry_path,
                report.entry_zvol,
                report.entry_cvd_z,
                report.entry_lsr_z,
                report.entry_adx,
                report.entry_kalman_r2,
            ),
        )
        await self._conn.commit()
        log.debug(
            "state_db_position_inserted",
            pid=position.position_id[:8],
            symbol=position.symbol,
            entry=round(position.entry_price, 4),
        )

    async def close_position(self, report: ClosedExecReport) -> None:
        """
        Marca uma posição como CLOSED com os dados reais de saída.

        Chamado ao receber ClosedExecReport do ExecutionEngine.

        v3.6: também persiste breakeven_active para consistência — quando o
        breakeven ativa e fecha no mesmo candle, o PositionStateUpdate nunca
        é emitido (só roda para posições que continuam abertas). Sem este
        UPDATE, posições fechadas por BREAKEVEN ficam com breakeven_active=0
        no DB apesar de close_reason='BREAKEVEN'.

        Args:
            report: ClosedExecReport com fill de saída, razão e PnL.
        """
        now_ms = int(time.time() * 1000)
        # v3.6: infere breakeven_active a partir do close_reason para garantir
        # consistência no DB mesmo quando o PositionStateUpdate não foi emitido.
        breakeven_active = 1 if report.close_reason == "BREAKEVEN" else None
        if breakeven_active is not None:
            await self._conn.execute(
                """
                UPDATE positions SET
                    status           = 'CLOSED',
                    close_reason     = ?,
                    close_price      = ?,
                    pnl_usdt         = ?,
                    pnl_pct          = ?,
                    closed_at        = ?,
                    breakeven_active = ?,
                    gross_pnl        = ?,
                    commission       = ?
                WHERE position_id = ?
                """,
                (
                    report.close_reason,
                    report.close_fill.fill_price,
                    report.pnl_usdt,
                    report.pnl_pct,
                    now_ms,
                    breakeven_active,
                    report.gross_pnl,   # v3.7: PnL antes da comissão
                    report.commission,  # v3.7: comissão bilateral estimada
                    report.position_id,
                ),
            )
        else:
            await self._conn.execute(
                """
                UPDATE positions SET
                    status       = 'CLOSED',
                    close_reason = ?,
                    close_price  = ?,
                    pnl_usdt     = ?,
                    pnl_pct      = ?,
                    closed_at    = ?,
                    gross_pnl    = ?,
                    commission   = ?
                WHERE position_id = ?
                """,
                (
                    report.close_reason,
                    report.close_fill.fill_price,
                    report.pnl_usdt,
                    report.pnl_pct,
                    now_ms,
                    report.gross_pnl,   # v3.7: PnL antes da comissão
                    report.commission,  # v3.7: comissão bilateral estimada
                    report.position_id,
                ),
            )
        await self._conn.commit()
        log.debug(
            "state_db_position_closed",
            pid=report.position_id[:8],
            symbol=report.symbol,
            reason=report.close_reason,
            pnl_usdt=round(report.pnl_usdt, 4),
        )

    async def mark_closed_no_fill(
        self,
        position_id: str,
        symbol:      str,
        reason:      str,
        close_price: Optional[float] = None,
    ) -> None:
        """
        Marca posição como CLOSED sem FillResult (ex: SL server-side detectado
        na reconciliação de startup — a posição saiu enquanto a engine estava down).

        pnl_usdt/pnl_pct ficam NULL neste caso (não temos o fill real).
        """
        now_ms = int(time.time() * 1000)
        await self._conn.execute(
            """
            UPDATE positions SET
                status       = 'CLOSED',
                close_reason = ?,
                close_price  = ?,
                closed_at    = ?
            WHERE position_id = ?
            """,
            (reason, close_price, now_ms, position_id),
        )
        await self._conn.commit()
        log.info(
            "state_db_position_closed_no_fill",
            pid=position_id[:8],
            symbol=symbol,
            reason=reason,
        )

    async def update_monitor_state(self, position: Position) -> None:
        """
        Atualiza os campos mutáveis de uma posição aberta a cada candle.

        Persiste o estado do trailing stop e MaxHold para que um restart
        recupere exatamente onde o monitor parou — sem reiniciar o contador
        de candles do zero nem perder o peak atingido.

        v3.7: também atualiza peak_price (máximo histórico de close para MFE).

        Chamado pelo StateManager a cada candle que modifica uma posição.
        """
        await self._conn.execute(
            """
            UPDATE positions SET
                sl_price         = ?,
                peak             = ?,
                candles_open     = ?,
                trailing_active  = ?,
                cvd_neg_streak   = ?,
                breakeven_active = ?,
                peak_price       = MAX(COALESCE(peak_price, 0), ?)
            WHERE position_id = ? AND status = 'OPEN'
            """,
            (
                position.sl_price,
                position.peak,
                position.candles_open,
                int(position.trailing_active),
                position.cvd_neg_streak,         # v3.3: streak do FlowCB
                int(position.breakeven_active),   # v3.4: flag breakeven
                position.peak,                   # v3.7: peak_price = MAX(existente, current)
                position.position_id,
            ),
        )
        await self._conn.commit()

    # ── Leitura ───────────────────────────────────────────────────────────────

    async def get_open_positions(self) -> list[tuple]:
        """
        Retorna todas as posições com status='OPEN' como lista de tuplas brutas.

        Inclui sl_order_id para restauração dos mapas de ordens no ExecutionEngine.
        O StateManager converte as tuplas em objetos Position.
        """
        async with self._conn.execute(
            "SELECT * FROM positions WHERE status = 'OPEN' ORDER BY opened_at ASC"
        ) as cursor:
            rows = await cursor.fetchall()
        log.info(
            "state_db_open_positions_loaded",
            count=len(rows),
        )
        return [dict(row) for row in rows]

    async def count_open(self) -> int:
        """Conta posições abertas. Útil para logs de startup."""
        async with self._conn.execute(
            "SELECT COUNT(*) FROM positions WHERE status = 'OPEN'"
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else 0

    async def get_recent_trades(self, limit: int = 50) -> list[dict]:
        """
        Retorna os N trades fechados mais recentes para o dashboard.

        Returns:
            Lista de dicts com campos: symbol, close_reason, entry_price,
            close_price, pnl_usdt, pnl_pct, candles_open, tier, closed_at
        """
        async with self._conn.execute(
            """
            SELECT symbol, close_reason, entry_price, close_price,
                   pnl_usdt, pnl_pct, candles_open, tier, closed_at
            FROM positions
            WHERE status = 'CLOSED' AND pnl_usdt IS NOT NULL
            ORDER BY closed_at DESC
            LIMIT ?
            """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_stats(self) -> dict:
        """
        Estatísticas básicas do histórico de trades para logging de startup.

        Returns:
            dict com total_trades, win_rate, avg_pnl_usdt, total_pnl_usdt
        """
        async with self._conn.execute(
            """
            SELECT
                COUNT(*)                                          AS total,
                SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END)    AS wins,
                AVG(pnl_usdt)                                     AS avg_pnl,
                SUM(pnl_usdt)                                     AS total_pnl
            FROM positions
            WHERE status = 'CLOSED' AND pnl_usdt IS NOT NULL
            """
        ) as cursor:
            row = await cursor.fetchone()

        if not row or not row[0]:
            return {"total_trades": 0, "win_rate": 0.0, "avg_pnl_usdt": 0.0, "total_pnl_usdt": 0.0}

        total    = row[0] or 0
        wins     = row[1] or 0
        avg_pnl  = row[2] or 0.0
        total_pnl = row[3] or 0.0

        return {
            "total_trades":  total,
            "win_rate":      round(wins / total * 100, 1) if total > 0 else 0.0,
            "avg_pnl_usdt":  round(avg_pnl, 4),
            "total_pnl_usdt": round(total_pnl, 4),
        }
