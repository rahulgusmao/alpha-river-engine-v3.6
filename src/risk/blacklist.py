"""
src/risk/blacklist.py
Symbol Blacklist — Alpha River v3.3

Motivação:
  Análise per-símbolo do backtest Jan-Fev 2026 (in-sample) revelou 20 símbolos
  com PnL acumulado negativo desproporcional. Esses símbolos somaram -$1.399,31
  em PnL, representando ~26% das perdas totais do período.

  Excluir esses 20 símbolos do universo tradeable reduziu o MaxDD de 54.4% para
  41.0% in-sample (MH24 com vs sem blacklist) e é o segundo maior contribuidor
  isolado para a melhoria do PF (após o SL Decay).

  Impacto isolado da Blacklist (MH24+BL vs MH24 sem BL):
    PnL:   -$5.373 → -$3.973  (+$1.399)
    MaxDD:  54.4%  →  40.5%   (-14pp)

Manutenção:
  A blacklist deve ser revisada a cada 30–60 dias usando o backtest per-símbolo
  OOS. Símbolos que se recuperam devem ser removidos; novos draggers adicionados.
  Próxima revisão recomendada: maio 2026 (após 60 dias OOS acumulados).

Símbolos (20 — piores por PnL acumulado Jan-Fev 2026):
  LITUSDT, EIGENUSDT, SUSDT, APTUSDT, SOONUSDT, GIGGLEUSDT, ORDIUSDT, ARBUSDT,
  BREVUSDT, FUNUSDT, BLESSUSDT, 1INCHUSDT, SAGAUSDT, NEIROUSDT, TAOUSDT,
  NOTUSDT, BIOUSDT, ETHFIUSDT, ALTUSDT, BEATUSDT
"""

import logging
from typing import FrozenSet

logger = logging.getLogger(__name__)


# ─── Blacklist canônica v3.3 — derivada do backtest Jan-Fev 2026 ────────────
# Ordenada por PnL acumulado ascendente (piores primeiro).
# Fonte: fase123_report.xlsx, aba "Blacklist", 2026-04-04.
_BLACKLIST_V33: FrozenSet[str] = frozenset({
    "LITUSDT",
    "EIGENUSDT",
    "SUSDT",
    "APTUSDT",
    "SOONUSDT",
    "GIGGLEUSDT",
    "ORDIUSDT",
    "ARBUSDT",
    "BREVUSDT",
    "FUNUSDT",
    "BLESSUSDT",
    "1INCHUSDT",
    "SAGAUSDT",
    "NEIROUSDT",
    "TAOUSDT",
    "NOTUSDT",
    "BIOUSDT",
    "ETHFIUSDT",
    "ALTUSDT",
    "BEATUSDT",
})


class SymbolBlacklist:
    """
    Filtro de símbolos bloqueados para entrada.

    Uso pelo RiskManager (pré-entrada):

        blacklist = SymbolBlacklist()
        if blacklist.is_blocked(signal.symbol):
            return  # ignorar sinal

    Permite override via config para testes A/B:

        blacklist = SymbolBlacklist(enabled=False)   # desativa em testes
        blacklist = SymbolBlacklist(extra={"XYZUSDT"})  # adiciona temporariamente
    """

    def __init__(
        self,
        enabled: bool = True,
        symbols: FrozenSet[str] | None = None,
        extra: set[str] | None = None,
    ) -> None:
        """
        Args:
            enabled: Se False, nenhum símbolo é bloqueado (útil para testes A/B).
            symbols: Conjunto customizado de símbolos bloqueados.
                     Se None, usa _BLACKLIST_V33 (padrão).
            extra:   Símbolos adicionais a bloquear além do conjunto base.
        """
        self.enabled = enabled
        base = symbols if symbols is not None else _BLACKLIST_V33
        self._blocked: FrozenSet[str] = base | frozenset(extra or set())

        logger.info(
            "SymbolBlacklist inicializada | enabled=%s | %d símbolos bloqueados",
            enabled, len(self._blocked) if enabled else 0,
        )

    @classmethod
    def from_config(cls, risk_cfg: dict) -> "SymbolBlacklist":
        """
        Cria instância a partir do bloco risk do engine.yaml.

        Chaves esperadas (opcionais):
            blacklist_enabled (bool): default True
            blacklist_extra   (list): símbolos adicionais a bloquear
        """
        enabled = risk_cfg.get("blacklist_enabled", True)
        extra_raw = risk_cfg.get("blacklist_extra", [])
        extra = set(s.upper() for s in extra_raw)
        return cls(enabled=enabled, extra=extra)

    def is_blocked(self, symbol: str) -> bool:
        """
        Retorna True se o símbolo está na blacklist e a blacklist está ativada.

        Args:
            symbol: Par de negociação (ex: "LITUSDT"). Case-insensitive.

        Returns:
            bool: True → bloquear entrada; False → permitir.
        """
        if not self.enabled:
            return False
        blocked = symbol.upper() in self._blocked
        if blocked:
            logger.debug("Blacklist: %s BLOQUEADO", symbol)
        return blocked

    def filter_universe(self, symbols: list[str]) -> list[str]:
        """
        Filtra uma lista de símbolos, removendo os bloqueados.

        Args:
            symbols: Lista de pares de negociação.

        Returns:
            Lista com símbolos não bloqueados.
        """
        if not self.enabled:
            return symbols
        filtered = [s for s in symbols if not self.is_blocked(s)]
        removed = len(symbols) - len(filtered)
        if removed:
            logger.info("Blacklist: %d símbolo(s) removido(s) do universo", removed)
        return filtered

    @property
    def blocked_symbols(self) -> FrozenSet[str]:
        """Retorna o conjunto de símbolos bloqueados (somente leitura)."""
        return self._blocked if self.enabled else frozenset()

    def __len__(self) -> int:
        return len(self._blocked) if self.enabled else 0

    def __repr__(self) -> str:
        return f"SymbolBlacklist(enabled={self.enabled}, count={len(self)})"
