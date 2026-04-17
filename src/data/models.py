"""
src/data/models.py
Modelos de dados brutos produzidos pelo Data Layer.
São os contratos de interface entre o Data Layer e o Feature Engine.
Nenhum cálculo de feature aqui — apenas estruturas de transporte.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass
class RawCandle:
    """
    Candle OHLCV fechado, produzido pelo WebSocket @kline_15m.

    O campo taker_buy_base_vol corresponde ao campo 'V' do evento de kline
    da Binance Futures — é a fonte primária do CVD real (tbv).
    """

    symbol: str
    ts_open: int               # epoch ms — abertura da barra
    ts_close: int              # epoch ms — fechamento da barra
    open: float
    high: float
    low: float
    close: float
    volume: float              # volume total em base asset (campo 'v')
    taker_buy_base_vol: float  # volume taker buy em base asset (campo 'V')
    num_trades: int
    interval: str              # ex: '15m'

    @property
    def taker_sell_base_vol(self) -> float:
        """Volume de sell taker = volume total - taker buy."""
        return self.volume - self.taker_buy_base_vol

    @property
    def cvd_delta(self) -> float:
        """
        CVD delta da barra: pressão compradora - pressão vendedora.
        Positivo = takers comprando mais do que vendendo.
        """
        return self.taker_buy_base_vol - self.taker_sell_base_vol

    @property
    def datetime_close(self) -> datetime:
        """Timestamp de fechamento como datetime UTC."""
        return datetime.fromtimestamp(self.ts_close / 1000, tz=timezone.utc)

    def __repr__(self) -> str:
        return (
            f"RawCandle({self.symbol} | {self.datetime_close.isoformat()} | "
            f"close={self.close:.4f} | vol={self.volume:.2f} | cvd={self.cvd_delta:.2f})"
        )


@dataclass
class RawOI:
    """
    Open Interest atual em base asset (número de contratos abertos).
    Obtido via REST /fapi/v1/openInterest no fechamento de cada barra.
    """

    symbol: str
    ts: int          # epoch ms — momento da leitura
    open_interest: float

    def __repr__(self) -> str:
        return f"RawOI({self.symbol} | oi={self.open_interest:.2f})"


@dataclass
class RawLSR:
    """
    Long/Short Ratio de contas globais (varejo).
    Obtido via REST /futures/data/globalLongShortAccountRatio.
    Ratio > 1.0 = mais longs do que shorts no varejo.
    """

    symbol: str
    ts: int          # epoch ms
    long_short_ratio: float

    def __repr__(self) -> str:
        return f"RawLSR({self.symbol} | lsr={self.long_short_ratio:.4f})"


@dataclass
class RawBTCD:
    """
    BTC Dominance em percentual (0–100), proveniente da CoinGecko.
    Usado exclusivamente pelo Kill Switch — não é sinal de entrada.
    Delay de ~5min é aceitável para esta finalidade.
    """

    ts: int          # epoch ms
    btc_dominance: float  # ex: 54.3 significa 54.3%

    def __repr__(self) -> str:
        return f"RawBTCD(dominance={self.btc_dominance:.2f}%)"


@dataclass
class ClosedCandle:
    """
    Evento completo de candle fechado — todas as fontes de dados consolidadas.

    Produzido pelo StreamManager após enriquecimento com OI e LSR.
    Consumido pelo Feature Engine via asyncio.Queue.

    oi e lsr podem ser None se o fetch REST falhar — o Feature Engine
    deve tratar esses casos (ex: usar valor anterior ou pular o candle).
    """

    candle: RawCandle
    oi: Optional[RawOI] = None
    lsr: Optional[RawLSR] = None

    @property
    def symbol(self) -> str:
        return self.candle.symbol

    @property
    def ts_close(self) -> int:
        return self.candle.ts_close

    def __repr__(self) -> str:
        return (
            f"ClosedCandle({self.candle.symbol} | "
            f"close={self.candle.close:.4f} | "
            f"oi={'ok' if self.oi else 'None'} | "
            f"lsr={'ok' if self.lsr else 'None'})"
        )
