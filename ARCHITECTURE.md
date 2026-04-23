# Alpha River Engine — Arquitetura & Histórico de Evolução

> Documento vivo. Atualizado a cada versão com decisões técnicas, evidências
> empíricas e motivação das mudanças. Data de início: 2026-04-04.

---

## Visão Geral do Sistema

Pipeline assíncrono em Python para trading algorítmico em Binance Futures (USDT-M Perpétuos).

```
Binance WS/REST
      │
      ▼
 DataLayer          ← recebe klines 15m, OI 1h, LSR, funding
      │
      ▼
 FeatureEngine      ← computa ZVol, CVD, LSR_n, ADX, RSI, Kalman R²
      │ (fanout)
      ├──────────────────────────────────────────┐
      ▼                                          ▼
 SignalEngine        ← score ≥ 0.40 → Signal    ExecutionEngine candle loop
      │                                          │
      ▼                                          ▼
 RiskManager         ← valida entrada           RiskManager avalia posições abertas
      │                                          │
      ▼                                          ▼
 ExecutionEngine     ← envia OrderSpec           ExecutionEngine fecha posição
      │
      ▼
 StateManager        ← persiste em SQLite (alpha_river_*.db)
```

---

## Parâmetros Canônicos (atuais — v3.6 Rev.1)

| Parâmetro | Valor | Origem |
|---|---|---|
| `score_threshold` | 0.40 | Pine v4.2 Golden Final |
| `sl_atr_multiplier` | 2.0 | v3.3 (grid empírico Jan-Fev 2026) |
| `trailing_activation_ratio` | **1.002** | v3.5 (era 1.003) |
| `trailing_capture_ratio` | **0.90** | v3.5 (era 0.80) |
| `max_hold_candles` | **8** | v3.6 (era 20 em v3.3, 24 em v3.2) |
| `breakeven_trigger_candles` | **4** | v3.6 (era 8 em v3.4) |
| `breakeven_loss_cap_pct` | **0.50** | v3.6 (novo) |
| `flow_cb_enabled` | **false** | v3.5 (era true) |
| `commission_factor` | **0.0008** | v3.6 Rev.1 (novo — 0.04% taker × 2 lados) |
| `sl_decay_phase1_candles` | 4 | v3.3 |
| `sl_decay_phase2_candles` | 16 | v3.3 |
| `sl_decay_mult_phase1` | 2.0 | v3.3 |
| `sl_decay_mult_phase2` | 1.7 | v3.3 |
| `sl_decay_mult_phase3` | 0.8 | v3.3 |
| `d2_atr_entry_max_ratio` | 0.30 | v3.3 |
| `leverage` | 3× | v3.2 |
| `risk_per_trade_pct` | 1% | v3.2 |

---

## Histórico de Versões

---

### v3.2 — Baseline (antes de 2026-04-04)

**Arquitetura:** Pipeline básico com score-based entry, trailing stop gain-based e MaxHold como saída de último recurso.

**Módulos de risco ativos:**
- `Sizer` — position sizing 1% do capital por trade
- `StopLossCalculator` — SL = entry − 2.0 × ATR(14)
- `TrailingStop` — ativa em +0.3%, captura 80% do peak
- `MaxHoldChecker` — fecha após 24 candles (6h)
- `KillSwitch` — BTC.D drop >3% em 4h ou drawdown >10%

**Resultado empírico (paper trading):**
- WR ~62%, PF ~0.80, MH dominante no fechamento de perdedores

---

### v3.3 — Módulos de Filtragem (2026-04-04)

**Motivação:** Backtest Fase 1+2+3 (Jan-Fev 2026, Top 200 símbolos) identificou:
1. 20 símbolos cronicamente perdedores responsáveis por -$X de drawdown
2. Símbolos de ATR extremo (>30% do preço) com risco desproporcionado
3. Trades mantidos muito tempo sem movimento (MaxHold excessivo)
4. Necessidade de SL dinâmico que acompanhe o envelhecimento do trade

**Mudanças implementadas:**

| Módulo | Arquivo | Descrição |
|---|---|---|
| `SymbolBlacklist` | `src/risk/blacklist.py` | 20 piores símbolos Jan-Fev 2026 bloqueados |
| `DecaySLChecker` | `src/risk/decay_sl.py` | SL tightens em 3 fases usando entry_atr fixo |
| `FlowCircuitBreaker` | `src/risk/flow_cb.py` | Fecha se CVD_n < -0.9 por 4c consecutivos + close < entry |
| D2 ATR Filter | `risk_manager.py` | Rejeita entrada se ATR/price > 30% |
| `max_hold_candles` | config | 24 → 20 (ótimo empírico: abaixo de 20 piora WR) |

**Bugs de integração encontrados e corrigidos (2026-04-09):**
1. `entry_atr` não propagado via `ExecReport` → `Position`
2. Schema SQLite sem colunas `entry_atr`, `cvd_neg_streak`
3. `on_candle()` não passava features para o RiskManager
4. `FeatureEngine` sem cache `_last_features` acessível
5. `StateManager.recover_and_inject()` não restaurava campos novos

---

### v3.4 — Breakeven Stop (2026-04-09)

**Motivação:** Diagnóstico live (Apr 2026) revelou:
- 85 trades fechados por MAX_HOLD, WR=8.2%, -98.5 USDT acumulado
- Distribuição de winners: P25=2c, P50=3c, **P75=7c**, P90=14c
- Trade que chega ao candle 8 sem trailing ativado = zumbi estatístico

**Lógica implementada (`src/risk/breakeven.py`):**
```
Se candles_open >= 8 E trailing_active = False:
  Se close <= entry_price → fecha (BREAKEVEN)
  Senão              → move SL para entry_price (breakeven_active = True)
```

**Efeito medido no backtest:** 38% dos trades passam pelo Breakeven Stop.
O módulo absorveu o papel do FLOW_CB — trades que deterioram chegam ao c8
com CVD negativo, mas o Breakeven já moveu o SL antes do streak se completar.

**Schema SQLite:** adicionada coluna `breakeven_active INTEGER NOT NULL DEFAULT 0`
com migração idempotente em `_MIGRATIONS_V34`.

---

### v3.5 — Trailing Recalibrado + FLOW_CB Desativado (2026-04-10)

**Motivação:** Grid search sistemático (180 símbolos, Jan-Fev 2026) em dois eixos:

**Grid 1 — SL Multiplier × FLOW_CB (85 combinações):**
- `sl_init` ∈ {1.0, 1.2, 1.3, 1.5, 2.0}
- `flow_cvd` ∈ {-0.85, -0.90, -0.95, -0.97, OFF}
- `flow_streak` ∈ {3, 4, 6, 8}

Resultado: FLOW_CB disparou em **0.08% dos trades** em todas as 85 configs.
O Breakeven c8 absorve os trades antes do streak CVD se completar.
Variação de PnL entre todas as 85 configs: apenas **$190** — ruído puro.
Conclusão: FLOW_CB é inerte com Breakeven ativo. **Desativado.**

**Grid 2 — TRAILING_ACTIVATION × TRAILING_CAPTURE (25 combinações):**
- `activation` ∈ {+0.2%, +0.3%, +0.5%, +0.8%, +1.0%}
- `capture` ∈ {70%, 75%, 80%, 85%, 90%}

Heatmap de Profit Factor (extrato):

```
act \ cap   70%     75%     80%     85%     90%
+0.2%      0.819   0.828  [0.842]  0.860  [0.870] ← ótimo
+0.3%      0.816   0.823   0.837   0.853   0.865  ← baseline
+0.5%      0.819   0.826   0.840   0.854   0.863
+0.8%      0.814   0.818   0.830   0.842   0.850
+1.0%      0.807   0.814   0.823   0.833   0.843
```

Heatmap de TRAILING% dos trades (quanto do universo ativa trailing):

```
act \ cap   70%    75%    80%    85%    90%
+0.2%      66.1%  66.2%  66.3%  66.3%  66.3%  ← mais trades capturam trending
+0.3%      60.7%  60.9%  61.0%  61.1%  61.0%  ← baseline
+0.5%      52.0%  52.3%  52.4%  52.5%  52.4%
+0.8%      41.0%  41.5%  41.8%  41.9%  41.9%
+1.0%      35.2%  35.9%  36.3%  36.4%  36.4%
```

**Insight W/L Ratio:** Activation mais alto produz W/L < 1.0 (winners ganham mais
que losers perdem em termos unitários) mas pior PnL total — porque menos trades
ativam o trailing e mais escoam pelo Breakeven sem lucro. O que importa não é só
o ratio, mas **quantos trades chegam ao trailing**.

**Mudanças aplicadas:**

| Parâmetro | Antes | Depois | Impacto esperado |
|---|---|---|---|
| `trailing_activation_ratio` | 1.003 (+0.3%) | **1.002 (+0.2%)** | +5pp de trades via trailing (61%→66%) |
| `trailing_capture_ratio` | 0.80 (80%) | **0.90 (90%)** | avg_win maior, menor devolução de peak |
| `flow_cb_enabled` | true | **false** | remove ruído, simplifica pipeline |

**Resultado esperado (backtest Jan-Fev 2026):**
- PF: 0.837 → **0.870** (+3.9%)
- PnL: -$6.325 → **-$4.947** (+$1.378 no período histórico)
- TRAILING%: 61% → **66%** dos trades
- BREAKEVEN%: 38.8% → **33.6%** dos trades

**Nota sobre PnL negativo em Jan-Fev 2026:** O período histórico pode ter sido
estruturalmente desfavorável ao sistema. O paper trading live (Apr 2026) com
configuração similar produziu +83 USDT em 1.012 trades com WR=74.8% — divergência
que merece investigação após acúmulo de dados com v3.5.

---

### v3.6 — Breakeven Antecipado + Cap de Loss + MaxHold Reduzido (2026-04-13)

**Motivação:** Diagnóstico do snapshot live (7.142 trades, 11-13/Abr/2026) revelou:
- 2.492 trades fechados como "BREAKEVEN" com P&L total de **-1.724 USDT** — o maior drain da engine.
- O mecanismo v3.4 (trigger c8) fechava com loss de -8% a -11% porque o preço já estava muito abaixo do entry no momento do trigger — o SL nunca chegava ao entry a tempo.
- 100% dos trades vencedores via TRAILING fecham em ≤ 4 candles. Pós-c4 sem trailing = zumbi estatístico.
- Segurar além de 8 candles é **exponencialmente prejudicial**: cada candle adicional após o c4 aumenta o loss esperado sem incremento proporcional de WR.

**Grid search sobre dados reais (2.604 trades afetados, 55h de operação):**

| Cap de loss | P&L simulado | Delta vs original |
|---|---|---|
| 0.25% | +1.417 USDT | +1.374 |
| **0.50%** | **+1.127 USDT** | **+1.084** |
| 0.75% | +899 USDT | +856 |
| 1.00% | +724 USDT | +681 |

Cap 0.50% selecionado: equilíbrio entre proteção e ruído. Com loss médio de -0.10%/candle, o cap de 0.50% levaria ~4.9 candles para ser atingido — alinhado com a janela c4→c8.

**Nova lógica do Breakeven (v3.6):**
```
c0-c3: Comportamento normal (SL inicial, DecaySL, Trailing)
c4+:   BE TRIGGER (sem trailing ativo)
         → SL move para entry_price
         → Se close <= entry × (1 - 0.50%) → fecha BREAKEVEN (cap de loss)
         → Se close <= entry               → fecha BREAKEVEN (SL no entry tocado)
         → Se close > entry               → trade continua com SL no entry
c8:    MAX_HOLD fecha qualquer posição ainda aberta
```

**Robustez por dia (cap 0.50%):**

| Dia | P&L original | P&L simulado | Delta |
|---|---|---|---|
| 2026-04-11 | -515 USDT | -213 USDT | +301 USDT |
| 2026-04-12 | -920 USDT | -320 USDT | +600 USDT |
| 2026-04-13 | -287 USDT | -105 USDT | +182 USDT |

**Mudanças aplicadas:**

| Parâmetro | Antes | Depois | Impacto esperado |
|---|---|---|---|
| `breakeven_trigger_candles` | 8 | **4** | Intercepta zumbis 4 candles mais cedo |
| `breakeven_loss_cap_pct` | N/A | **0.50%** | Cap de loss máximo entre c4 e c8 |
| `max_hold_candles` | 20 | **8** | Elimina 12 candles de exposição inútil |

**Resultado esperado (simulação sobre 55h de dados live):**
- P&L total: +42 USDT → **+1.127 USDT** (+2.551%)
- Trades salvos do loss ilimitado: **1.657 de 2.604** (63.6%)
- BREAKEVEN% (agora com perda real limitada): trades entre c4-c8 com loss ≤ 0.50%

---

### v3.6 Rev.1 — Ciclo de Correções Pós-Diagnóstico (2026-04-13)

**Motivação:** Diagnóstico sistemático do snapshot live (7.142 trades, 11-13/Abr/2026) identificou 8 inconformidades entre a implementação e os princípios do framework. As 6 primeiras foram corrigidas em sequência imediatamente após v3.6. INC-7 e INC-8 completam o ciclo.

**Inconformidades identificadas e resolvidas:**

| INC | Arquivo | Problema | Correção |
|---|---|---|---|
| INC-1 | `breakeven.py`, `risk_manager.py`, configs | BE trigger c8 fechava com loss -8% a -11%; sem cap de loss | Trigger → c4, cap 0.50%, max_hold → 8. Implementação completa da v3.6 |
| INC-2 | `position_monitor.py` `check_sl_breach()` | Trades com `breakeven_active=True` batendo no SL do entry eram rotulados `SL` em vez de `BREAKEVEN` | Cadeia de precedência: `trailing_active → TRAILING` > `breakeven_active → BREAKEVEN` > `SL` |
| INC-3 | `database.py` `close_position()` | `breakeven_active` não era persistido no DB quando o BE ativava e fechava no mesmo candle (sem `PositionStateUpdate`) | `close_position()` agora inclui `breakeven_active=1` no UPDATE quando `close_reason='BREAKEVEN'` |
| INC-4 | `trailing.py`, `risk_manager.py` | Defaults hardcoded `activation=1.003 / capture=0.80` em `TrailingStop.__init__()` e no RiskManager — divergência com parâmetros v3.5 do YAML | Defaults atualizados para `1.002 / 0.90`. Default `max_hold_candles` no RiskManager: `20 → 8` |
| INC-5 | `risk_manager.py` linha 119 | `flow_cb_enabled` default `True` — se YAML ausente, FlowCB rodaria ativo contradizendo decisão v3.5 | Default alterado para `False` |
| INC-6 | `risk_manager.py` `evaluate_positions()` | `pos.cvd_neg_streak = 0` executado a cada candle quando FlowCB desativado — write desnecessário no DB | Reset removido; campo permanece 0 desde a abertura (default) |
| INC-7 | Dados históricos | 109 trades rotulados `SL` com `breakeven_active=1` e `entry_price==close_price==pnl_usdt=0` | Decisão: aceitar dados históricos sujos; INC-2 garante dados futuros corretos. Sem migration SQL |
| INC-8 | `execution_engine.py` `_handle_close()` | PnL calculado sem deduzir comissão de corretagem — resultado inflado em +$351,84 USDT nos 7.142 trades do snapshot | Fator bilateral `commission = entry_price × qty × 0.0008` deduzido do gross_pnl. Parâmetro `commission_factor` configurável no YAML |

**Revelação crítica do INC-8:** Com a comissão corrigida, o PnL bruto do período 11-13/Abr/2026 era apenas **+$42,52 USDT**. Após dedução das comissões (**-$351,84 USDT**), o período teria resultado em **-$309,32 USDT**. A engine gera edge no gross, mas as comissões superam o resultado bruto — esta é a evidência mais importante para a próxima fase de calibração: o Profit Factor precisa ser significativamente maior que 1.0 para sobreviver ao custo de transação em dry run.

**Arquivos modificados neste ciclo:**

| Arquivo | Tipo de mudança |
|---|---|
| `src/risk/breakeven.py` | Reescrito — INC-1 |
| `src/risk/max_hold.py` | Default `8` — INC-1 |
| `src/risk/trailing.py` | Defaults `1.002/0.90`, docstring — INC-4 |
| `src/risk/risk_manager.py` | Defaults trailing/max_hold/flow_cb, remoção reset cvd — INC-4/5/6 |
| `src/execution/position_monitor.py` | Cadeia de precedência `check_sl_breach()` — INC-2 |
| `src/execution/execution_engine.py` | Dedução de comissão bilateral, log gross/commission — INC-8 |
| `src/state/database.py` | `close_position()` persiste `breakeven_active` — INC-3 |
| `config/engine.yaml` | `commission_factor: 0.0008`, header v1.3 — INC-8 |
| `config/mainnet_dryrun.yaml` | `commission_factor: 0.0008` — INC-8 |
| `ARCHITECTURE.md` | Este documento |

---

## Próximas Investigações (Backlog)

| Prioridade | Item | Hipótese |
|---|---|---|
| Alta | Score threshold dinâmico | score > 0.50 em regime NEUTRO pode melhorar WR |
| Alta | Validar v3.6 Rev.1 em live 48h+ | Confirmar que PnL líquido (pós-comissão) é positivo com parâmetros corrigidos |
| Alta | Aumentar Profit Factor bruto | Com commission=0.08%/lado, PF bruto precisa superar ~1.16 para breakeven líquido |
| Média | TRAILING_CAPTURE por tier | Tier 1 (BTC/ETH) pode tolerar capture > 90% |
| Média | Atualizar data lake ≥ Mar 2026 | Backtest Mar-Abr 2026 para validação out-of-sample |
| Baixa | Reativar FLOW_CB com parâmetros maiores | streak=12+, apenas se Breakeven for removido |
| Baixa | SL multiplier 1.5 com activation+0.2% | Curva U no SL sugere interação com activation |

---

## Blacklist v3.3 (20 símbolos)

Piores símbolos por PnL total em Jan-Fev 2026 (Top 200 USDT Perps):

```
LITUSDT, EIGENUSDT, SUSDT, APTUSDT, SOONUSDT, GIGGLEUSDT, ORDIUSDT,
ARBUSDT, BREVUSDT, FUNUSDT, BLESSUSDT, 1INCHUSDT, SAGAUSDT, NEIROUSDT,
TAOUSDT, NOTUSDT, BIOUSDT, ETHFIUSDT, ALTUSDT, BEATUSDT
```

Revisão programada: após acúmulo de 60+ dias de dados live (≥ Jun 2026).

---

## Arquivos-Chave

```
alpha-river-engine/
├── config/
│   ├── engine.yaml              ← configuração canônica (testnet)
│   └── mainnet_dryrun.yaml      ← dry run com dados reais de mainnet
├── src/
│   ├── data/                    ← DataLayer: WS klines, REST OI/LSR
│   ├── features/
│   │   └── feature_engine.py    ← ZVol, CVD, LSR_n, ADX, Kalman; cache _last_features
│   ├── signals/                 ← SignalEngine: score + trigger
│   ├── risk/
│   │   ├── risk_manager.py      ← orquestrador principal (v3.5)
│   │   ├── models.py            ← Position, CloseReason, OrderSpec, CloseInstruction
│   │   ├── decay_sl.py          ← SL Time-Decay 3 fases (v3.3)
│   │   ├── breakeven.py         ← Breakeven Stop c4 + cap 0.50% (v3.6)
│   │   ├── flow_cb.py           ← Flow Circuit Breaker (v3.3, inativo em v3.5)
│   │   ├── blacklist.py         ← SymbolBlacklist 20 símbolos (v3.3)
│   │   ├── trailing.py          ← TrailingStop gain-based (act=1.002, cap=0.90, v3.5)
│   │   ├── max_hold.py          ← MaxHoldChecker (MH=8, v3.6)
│   │   ├── sizer.py             ← position sizing 1% capital
│   │   └── stop_loss.py         ← SL inicial = entry − mult × ATR
│   ├── execution/
│   │   └── execution_engine.py  ← fanout candle→feature_queue + exec_candle_queue
│   └── state/
│       ├── database.py          ← SQLite com migrações idempotentes v3.3/v3.4
│       └── state_manager.py     ← persiste/restaura posições abertas
├── main.py                      ← entry point; wiring de todos os componentes
└── ARCHITECTURE.md              ← este arquivo
```

---

*Última atualização: 2026-04-13 | Versão da engine: v3.6 Rev.1*
