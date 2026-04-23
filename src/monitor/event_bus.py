"""
src/monitor/event_bus.py
Bus de eventos assíncrono para broadcasting de eventos da engine ao dashboard.

Padrão pub/sub simples: publishers chamam publish() com um dict JSON-serializable;
subscribers recebem via asyncio.Queue retornada por subscribe().

Eventos publicados:
  position_opened  — nova posição aberta (ExecReport processado pelo StateManager)
  position_closed  — posição fechada (ClosedExecReport processado pelo StateManager)
  price_update     — preço mais recente por símbolo (cada ClosedCandle)
  portfolio_update — portfolio_value e open_positions atual da engine
  kill_switch      — status do kill switch (ativado/desativado)

Política de drops:
  Subscribers lentos recebem drops silenciosos (QueueFull ignorado).
  O dashboard é um consumidor de melhor esforço — perder um evento de preço
  não é crítico; ele fará refresh periódico de qualquer forma.
"""

import asyncio
from typing import Any


class EventBus:
    """
    Bus de eventos assíncrono para monitoramento em tempo real.

    Thread-safe para uso em asyncio (não para threading nativo).
    Todas as operações são no-op se não há subscribers — zero overhead
    quando o dashboard está desativado.
    """

    def __init__(self):
        self._subscribers: list[asyncio.Queue] = []

    def subscribe(self, maxsize: int = 500) -> asyncio.Queue:
        """
        Registra um novo subscriber e retorna sua Queue.

        Args:
            maxsize: capacidade da fila (drops silenciosos quando cheia)

        Returns:
            asyncio.Queue — o subscriber consome eventos desta fila.
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Remove um subscriber. Chamado quando WebSocket desconecta."""
        try:
            self._subscribers.remove(queue)
        except ValueError:
            pass

    def publish(self, event_type: str, data: Any) -> None:
        """
        Publica um evento para todos os subscribers.
        Non-blocking: put_nowait() com drop silencioso em fila cheia.

        Args:
            event_type: identificador do evento (ex: "position_opened")
            data:       payload JSON-serializable
        """
        if not self._subscribers:
            return   # nenhum subscriber — zero-cost early return

        event = {"type": event_type, "data": data}
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass   # subscriber lento — drop aceitável

    @property
    def subscriber_count(self) -> int:
        """Número de subscribers ativos."""
        return len(self._subscribers)
