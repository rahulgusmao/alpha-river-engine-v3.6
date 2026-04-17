"""
src/execution/position_monitor.py
Monitor de posições abertas — avaliação por candle fechado.

Responsabilidades:
  - Mantém o mapa position_id → Position das posições abertas
  - A cada ClosedCandle, delega avaliação de trailing/max_hold ao RiskManager
  - Expõe check_kill_switch() para o loop de candles da ExecutionEngine
  - Thread-safe via asyncio.Lock (posições são adicionadas/removidas concorrentemente)

Fluxo:
  ExecutionEngine._order_loop → add(position)
  ExecutionEngine._candle_loop → on_candle(candle) → [CloseInstruction, ...]
  ExecutionEngine._candle_loop → check_kill_switch(portfolio_value) → [CloseInstruction, ...]
"""

import asyncio
from typing import TYPE_CHECKING

import structlog

from src.data.models  import ClosedCandle
from src.risk.models  import CloseInstruction, CloseReason, Position

if TYPE_CHECKING:
    from src.risk.risk_manager import RiskManager

log = structlog.get_logger(__name__)


class PositionMonitor:
    """
    Gerencia o ciclo de vida das posições abertas em memória.

    Não executa ordens — apenas avalia condições de saída e retorna instruções.
    A execução das ordens de saída é responsabilidade da ExecutionEngine.
    """

    def __init__(self, risk_manager: "RiskManager"):
        self._rm = risk_manager
        # position_id → Position
        self._positions: dict[str, Position] = {}
        self._lock = asyncio.Lock()

    # ── Gestão de Posições ────────────────────────────────────────────────────

    async def add(self, position: Position) -> None:
        """Registra nova posição aberta no monitor."""
        async with self._lock:
            self._positions[position.position_id] = position
        log.info(
            "position_monitor_add",
            symbol=position.symbol,
            pid=position.position_id[:8],
            entry=position.entry_price,
            sl=round(position.sl_price, 4),
            total_open=len(self._positions),
        )

    async def remove(self, position_id: str) -> None:
        """Remove posição do monitor (chamada após confirmação de fechamento)."""
        async with self._lock:
            removed = self._positions.pop(position_id, None)
        if removed:
            log.debug(
                "position_monitor_remove",
                pid=position_id[:8],
                symbol=removed.symbol,
                remaining=len(self._positions),
            )

    # ── Avaliação por Candle ─────────────────────────────────────────────────

    async def on_candle(
        self,
        candle: ClosedCandle,
        features_by_symbol: dict | None = None,
    ) -> tuple[list[CloseInstruction], list[Position]]:
        """
        Processa um candle fechado para o símbolo correspondente.

        Avalia DECAY_SL, FlowCB, trailing stop e MaxHold para todas as posições
        do símbolo. Remove do monitor as posições que devem ser encerradas.

        Args:
            candle:             ClosedCandle recebido do DataLayer (via fanout)
            features_by_symbol: dict {symbol: {'cvd_n': float, ...}} — v3.3 FlowCB.
                                Se None ou símbolo ausente, FlowCB é ignorado.

        Returns:
            Tupla (close_instructions, updated_positions):
              - close_instructions: posições que devem ser encerradas
              - updated_positions:  posições que foram avaliadas mas continuam abertas
                                    (candles_open foi incrementado, peak/sl_price
                                     podem ter sido atualizados pelo trailing/decay)
        """
        async with self._lock:
            relevant = [
                p for p in self._positions.values()
                if p.symbol == candle.symbol
            ]
            if not relevant:
                return [], []

            closes       = {candle.symbol: candle.candle.close}
            instructions = self._rm.evaluate_positions(
                relevant, closes, features_by_symbol=features_by_symbol
            )

            # IDs que vão fechar
            closing_ids = {ci.position_id for ci in instructions}

            # Remove do monitor as posições que vão ser encerradas
            for ci in instructions:
                self._positions.pop(ci.position_id, None)

            # Posições que foram avaliadas mas continuam abertas (estado modificado
            # in-place por evaluate_positions: candles_open++, peak, sl_price, cvd_neg_streak)
            updated = [p for p in relevant if p.position_id not in closing_ids]

        return instructions, updated

    async def check_sl_breach(self, candle: ClosedCandle) -> list[CloseInstruction]:
        """
        Dry run mode only: simula o SL server-side verificando se o candle.low
        atingiu o sl_price de alguma posição aberta do símbolo.

        Em modo live, esse papel é do SL STOP_MARKET colocado na Binance.
        Em dry_run, não há ordens reais — verificamos a cada candle fechado.

        Lógica conservadora: fill ao sl_price (não ao candle.low — evita
        over-optimistic fill price em gaps).

        Args:
            candle: ClosedCandle recebido do DataLayer

        Returns:
            Lista de CloseInstruction para posições com SL atingido.
        """
        async with self._lock:
            instructions = []
            symbol = candle.candle.symbol
            candle_low = candle.candle.low

            for p in list(self._positions.values()):
                if p.symbol != symbol:
                    continue
                if candle_low <= p.sl_price:
                    self._positions.pop(p.position_id)
                    # Determina o motivo correto da saída por breach de SL:
                    #   - trailing_active → o SL foi movido pelo trailing → TRAILING
                    #   - breakeven_active → o SL foi movido para entry pelo BE → BREAKEVEN
                    #   - nenhum → SL original ou DecaySL → SL
                    # v3.6: sem essa distinção, trades que saem pelo SL no entry
                    # (após breakeven mover o stop) eram rotulados como SL genérico.
                    if p.trailing_active:
                        breach_reason = CloseReason.TRAILING
                    elif p.breakeven_active:
                        breach_reason = CloseReason.BREAKEVEN
                    else:
                        breach_reason = CloseReason.SL
                    instructions.append(
                        CloseInstruction(
                            position_id=p.position_id,
                            symbol=p.symbol,
                            reason=breach_reason,
                            close_price=p.sl_price,  # fill conservador no nível do stop
                        )
                    )
                    log.info(
                        "dry_run_sl_breach",
                        symbol=symbol,
                        pid=p.position_id[:8],
                        reason=breach_reason.value,
                        sl_price=round(p.sl_price, 4),
                        candle_low=round(candle_low, 4),
                        trailing_active=p.trailing_active,
                        breakeven_active=p.breakeven_active,
                    )
            return instructions

    async def check_kill_switch(
        self, portfolio_value: float
    ) -> list[CloseInstruction]:
        """
        Verifica o Kill Switch e emite instruções de encerramento para TODAS
        as posições abertas se ativado.

        Deve ser chamado a cada candle fechado (antes de on_candle ou depois —
        a ordem não importa pois são eventos independentes).

        Args:
            portfolio_value: valor atual do portfólio em USDT

        Returns:
            Lista de CloseInstruction (vazia se Kill Switch não ativo).
        """
        async with self._lock:
            all_positions = list(self._positions.values())
            if not all_positions:
                return []

            instructions = self._rm.evaluate_kill_switch(all_positions, portfolio_value)

            for ci in instructions:
                self._positions.pop(ci.position_id, None)

        return instructions

    # ── Propriedades ─────────────────────────────────────────────────────────

    @property
    def count(self) -> int:
        """Número de posições abertas no momento."""
        return len(self._positions)

    @property
    def symbols(self) -> set[str]:
        """Conjunto de símbolos com posição aberta."""
        return {p.symbol for p in self._positions.values()}

    def get_position(self, position_id: str) -> Position | None:
        """Retorna a posição pelo ID ou None se não encontrada."""
        return self._positions.get(position_id)

    def snapshot(self) -> list[Position]:
        """Retorna cópia da lista de posições abertas (para logging/StateManager)."""
        return list(self._positions.values())
