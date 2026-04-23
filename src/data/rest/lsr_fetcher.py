"""
src/data/rest/lsr_fetcher.py
Fetch de Long/Short Ratio (contas globais) atual para símbolos individuais ou em lote.

Endpoint: /futures/data/globalLongShortAccountRatio
Retorna o ratio de contas de varejo global (não apenas top traders).
Ratio > 1.0 = mais contas net-long do que net-short.

Executado pelo StreamManager após o fechamento de cada barra.
"""

import asyncio
from typing import Dict, List, Optional

import structlog

from src.data.models import RawLSR
from src.data.rest.binance_rest import BinanceRestClient

logger = structlog.get_logger(__name__)


class LSRFetcher:
    """
    Busca Long/Short Ratio atual via /futures/data/globalLongShortAccountRatio.
    Peso do endpoint: 1 por símbolo (limit=1).
    """

    def __init__(self, client: BinanceRestClient, period: str = "15m"):
        """
        Args:
            client: instância compartilhada do BinanceRestClient
            period: período de agregação (deve ser igual ao kline_interval)
        """
        self._client = client
        self._period = period

    async def fetch_one(self, symbol: str) -> Optional[RawLSR]:
        """
        Busca LSR atual para um único símbolo.
        Usa limit=1 para pegar apenas o registro mais recente.

        Returns:
            RawLSR preenchido, ou None se o request falhar.
        """
        data = await self._client.get(
            "/futures/data/globalLongShortAccountRatio",
            params={"symbol": symbol, "period": self._period, "limit": 1},
            weight=1,
        )
        if not data or not isinstance(data, list) or len(data) == 0:
            logger.warning("lsr_fetch_none", symbol=symbol)
            return None

        item = data[0]
        try:
            return RawLSR(
                symbol=item["symbol"],
                ts=int(item["timestamp"]),
                long_short_ratio=float(item["longShortRatio"]),
            )
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("lsr_parse_error", symbol=symbol, error=str(exc))
            return None

    async def fetch_batch(self, symbols: List[str]) -> Dict[str, Optional[RawLSR]]:
        """
        Busca LSR para um conjunto de símbolos em paralelo.

        Returns:
            Dict {symbol: RawLSR | None} — símbolos com falha retornam None.
        """
        tasks = {sym: asyncio.create_task(self.fetch_one(sym)) for sym in symbols}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        output: Dict[str, Optional[RawLSR]] = {}
        for symbol, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                logger.warning("lsr_batch_error", symbol=symbol, error=str(result))
                output[symbol] = None
            else:
                output[symbol] = result

        return output
