"""
src/data/rest/binance_rest.py
Cliente HTTP base assíncrono para Binance Futures REST API.

Responsabilidades:
  - Gerenciar sessão aiohttp com pool de conexões TCP
  - Controle de rate limiting por janela deslizante (peso por minuto)
  - Semáforo de concorrência para limitar requests simultâneos
  - Retry com backoff exponencial em erros transientes
  - Tratamento explícito de HTTP 429 (rate limit) e 418 (IP ban)
"""

import asyncio
import time
from typing import Any, Dict, Optional

import aiohttp
import structlog

logger = structlog.get_logger(__name__)

# Limite de peso por minuto (Binance Futures IP weight limit)
_RATE_LIMIT_PER_MINUTE = 1200
# Usamos 85% do limite como margem de segurança
_RATE_LIMIT_SAFE = int(_RATE_LIMIT_PER_MINUTE * 0.85)
# Janela de rate limit em segundos
_RATE_LIMIT_WINDOW = 60.0


class BinanceRestClient:
    """
    Cliente assíncrono para Binance Futures REST API.

    Thread-safety: seguro para uso concorrente via asyncio (não thread-safe).
    Deve ser instanciado uma vez e compartilhado entre todos os fetchers.
    Chamar close() ao encerrar a aplicação para liberar conexões.
    """

    def __init__(
        self,
        base_url: str,
        semaphore_limit: int = 50,
        max_retries: int = 3,
    ):
        """
        Args:
            base_url:        URL base da API (ex: 'https://fapi.binance.com')
            semaphore_limit: máximo de requests HTTP simultâneos
            max_retries:     tentativas antes de desistir de um request
        """
        self._base_url = base_url.rstrip("/")
        self._semaphore = asyncio.Semaphore(semaphore_limit)
        self._max_retries = max_retries
        self._session: Optional[aiohttp.ClientSession] = None

        # Estado do rate limiting
        self._weight_lock = asyncio.Lock()
        self._weight_used: int = 0
        self._window_start: float = time.monotonic()

    # ──────────────────────────── SESSÃO ───────────────────────────────────

    async def _get_session(self) -> aiohttp.ClientSession:
        """Retorna sessão ativa (cria se necessário)."""
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                limit=200,          # conexões TCP simultâneas no pool
                limit_per_host=100,
                ssl=True,
            )
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=15, connect=5),
                headers={"Content-Type": "application/json"},
            )
        return self._session

    # ──────────────────────── RATE LIMITING ────────────────────────────────

    async def _check_rate_limit(self, weight: int) -> None:
        """
        Bloqueia a coroutine se o peso acumulado na janela atual
        atingir o limite seguro (85% de 1200/min).
        Reseta o contador ao início de cada nova janela de 60s.
        """
        async with self._weight_lock:
            now = time.monotonic()
            elapsed = now - self._window_start

            # Nova janela: reset do contador
            if elapsed >= _RATE_LIMIT_WINDOW:
                self._weight_used = 0
                self._window_start = now
                elapsed = 0.0

            # Próximo de estourar: aguarda até o fim da janela atual
            if self._weight_used + weight >= _RATE_LIMIT_SAFE:
                wait_for = _RATE_LIMIT_WINDOW - elapsed
                logger.warning(
                    "rate_limit_throttle",
                    weight_used=self._weight_used,
                    limit=_RATE_LIMIT_SAFE,
                    wait_sec=round(wait_for, 2),
                )
                if wait_for > 0:
                    await asyncio.sleep(wait_for)
                self._weight_used = 0
                self._window_start = time.monotonic()

            self._weight_used += weight

    # ──────────────────────────── GET ──────────────────────────────────────

    async def get(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        weight: int = 1,
    ) -> Optional[Any]:
        """
        Executa GET assíncrono com rate limiting, controle de concorrência e retry.

        Args:
            path:   path relativo à base_url (ex: '/fapi/v1/klines')
            params: query parameters
            weight: peso do endpoint no rate limit da Binance

        Returns:
            JSON parseado (dict ou list) ou None em caso de falha definitiva.
        """
        url = f"{self._base_url}{path}"
        await self._check_rate_limit(weight)

        async with self._semaphore:
            _429_retries = 0
            _MAX_429_RETRIES = 3
            for attempt in range(1, self._max_retries + 1):
                try:
                    session = await self._get_session()
                    async with session.get(url, params=params) as resp:

                        if resp.status == 200:
                            return await resp.json()

                        elif resp.status == 429:
                            _429_retries += 1
                            if _429_retries > _MAX_429_RETRIES:
                                logger.error(
                                    "http_429_max_retries_exceeded",
                                    url=url,
                                    retries=_429_retries,
                                )
                                return None
                            # Rate limit atingido — Binance indica Retry-After
                            retry_after = int(resp.headers.get("Retry-After", 15))
                            logger.warning(
                                "http_429_rate_limited",
                                url=url,
                                retry_after=retry_after,
                                retry=_429_retries,
                            )
                            await asyncio.sleep(retry_after)

                        elif resp.status == 418:
                            # IP banido temporariamente — aguarda mais tempo
                            logger.error("http_418_ip_banned", url=url)
                            await asyncio.sleep(60)
                            return None

                        elif resp.status in (400, 404):
                            # Erro do cliente — não adianta tentar novamente
                            body = await resp.text()
                            logger.error(
                                "http_client_error",
                                status=resp.status,
                                url=url,
                                body=body[:200],
                            )
                            return None

                        else:
                            logger.warning(
                                "http_unexpected_status",
                                status=resp.status,
                                url=url,
                                attempt=attempt,
                            )

                except asyncio.TimeoutError:
                    logger.warning(
                        "request_timeout",
                        url=url,
                        attempt=attempt,
                        max_retries=self._max_retries,
                    )

                except aiohttp.ClientError as exc:
                    logger.warning(
                        "request_client_error",
                        url=url,
                        attempt=attempt,
                        error=str(exc),
                    )

                # Backoff exponencial entre tentativas (não na última)
                if attempt < self._max_retries:
                    backoff = 2.0 ** attempt
                    await asyncio.sleep(backoff)

            logger.error("request_all_retries_failed", url=url, attempts=self._max_retries)
            return None

    # ──────────────────────────── LIFECYCLE ────────────────────────────────

    async def close(self) -> None:
        """Fecha a sessão HTTP e libera conexões TCP do pool."""
        if self._session and not self._session.closed:
            await self._session.close()
            logger.debug("binance_rest_client_closed", base_url=self._base_url)
