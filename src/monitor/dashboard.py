"""
src/monitor/dashboard.py
Dashboard web em tempo real para monitorar posições, PnL e status da engine.

Arquitetura:
  - FastAPI + uvicorn rodando no mesmo event loop da engine (sem thread separada)
  - WebSocket /ws: recebe eventos do EventBus e envia snapshots periódicos
  - REST GET /api/snapshot: snapshot completo do estado atual (posições + stats)
  - GET /: serve o dashboard HTML single-page

Dados em tempo real:
  EventBus → DashboardServer._event_queue → WebSocket clients
  SQLite   → DashboardServer → /api/snapshot + snapshot periódico via WS

Preço atual:
  Atualizado via update_price(symbol, price) chamado pelo candle_fanout no main.py.
  Usado para calcular unrealized PnL no snapshot.

Uso:
    server = DashboardServer(config, state_manager, event_bus)
    asyncio.create_task(server.serve())
"""

import asyncio
import json
import time
from typing import TYPE_CHECKING

import structlog
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

if TYPE_CHECKING:
    from src.monitor.event_bus   import EventBus
    from src.state.state_manager import StateManager

log = structlog.get_logger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# HTML DO DASHBOARD
# ──────────────────────────────────────────────────────────────────────────────

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Alpha River — Live Monitor</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0e17;color:#c9d1d9;font-family:'Courier New',monospace;font-size:13px;min-height:100vh}
a{color:#58a6ff;text-decoration:none}

/* ── HEADER ── */
.header{background:#0d1117;border-bottom:1px solid #21262d;padding:10px 20px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.logo{color:#58a6ff;font-size:15px;font-weight:bold;letter-spacing:1px}
.badge{padding:2px 9px;border-radius:12px;font-size:11px;font-weight:bold;letter-spacing:.5px}
.badge-live{background:#1a7f3c;color:#3fb950}
.badge-dryrun{background:#5a3e00;color:#e3b341}
.badge-connecting{background:#1a2a3d;color:#58a6ff}
.spacer{flex:1}
.conn-status{display:flex;align-items:center;gap:6px;font-size:11px;color:#8b949e}
.conn-dot{width:8px;height:8px;border-radius:50%;display:inline-block}
.dot-ok{background:#3fb950}
.dot-err{background:#f85149}
.dot-wait{background:#e3b341;animation:pulse 1.2s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}

/* ── METRICS BAR ── */
.metrics{background:#0d1117;border-bottom:1px solid #21262d;padding:8px 20px;display:flex;gap:28px;flex-wrap:wrap}
.metric{display:flex;flex-direction:column;min-width:90px}
.metric-label{font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:1px;margin-bottom:2px}
.metric-value{font-size:17px;font-weight:bold;color:#c9d1d9}
.pos{color:#3fb950}.neg{color:#f85149}.neutral{color:#c9d1d9}

/* ── LAYOUT ── */
.layout{display:grid;grid-template-columns:1fr 300px;gap:12px;padding:12px 20px}
@media(max-width:900px){.layout{grid-template-columns:1fr}}

/* ── PANELS ── */
.panel{background:#0d1117;border:1px solid #21262d;border-radius:6px;overflow:hidden}
.panel-title{padding:8px 14px;background:#161b22;border-bottom:1px solid #21262d;font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:1px;display:flex;justify-content:space-between;align-items:center}
.panel-count{background:#21262d;color:#c9d1d9;border-radius:10px;padding:1px 7px;font-size:11px}

/* ── TABLES ── */
.tbl-wrap{overflow-x:auto;max-height:420px;overflow-y:auto}
table{width:100%;border-collapse:collapse}
th{padding:7px 10px;text-align:right;color:#8b949e;font-size:11px;font-weight:normal;border-bottom:1px solid #21262d;white-space:nowrap;position:sticky;top:0;background:#161b22;z-index:1}
th:first-child,td:first-child{text-align:left}
td{padding:6px 10px;text-align:right;border-bottom:1px solid #161b22;white-space:nowrap;font-size:12px}
tr:hover td{background:#161b22}
.empty-row td{text-align:center;color:#484f58;padding:20px;font-style:italic}

/* ── TAGS ── */
.tag{padding:1px 6px;border-radius:4px;font-size:10px;font-weight:bold;letter-spacing:.3px}
.tag-sl{background:#3d1a1a;color:#f85149}
.tag-trailing{background:#1a3d1a;color:#3fb950}
.tag-maxhold{background:#1a2d3d;color:#58a6ff}
.tag-ks{background:#3d2a1a;color:#e3b341}
.tag-active{background:#1a3d1a;color:#3fb950}
.tag-pending{background:#21262d;color:#8b949e}
.tag-t1{background:#1c2b1a;color:#56d364}.tag-t2{background:#1a2535;color:#58a6ff}.tag-t3{background:#2b1a30;color:#bc8cff}

/* ── SIDE PANEL ── */
.side-stack{display:flex;flex-direction:column;gap:12px}
.stat-grid{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:#21262d}
.stat-cell{background:#0d1117;padding:10px 12px}
.stat-label{font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:.8px;margin-bottom:4px}
.stat-value{font-size:16px;font-weight:bold}

/* ── TRADE LOG ── */
.trade-log{max-height:380px;overflow-y:auto}
.trade-item{padding:7px 14px;border-bottom:1px solid #161b22;display:flex;justify-content:space-between;align-items:center;gap:8px;font-size:12px}
.trade-item:last-child{border-bottom:none}
.trade-sym{font-weight:bold;color:#c9d1d9;min-width:80px}
.trade-pnl{font-weight:bold;min-width:70px;text-align:right}
.trade-meta{color:#8b949e;font-size:11px;text-align:right}

/* ── KS STATUS ── */
.ks-ok{color:#3fb950}.ks-active{color:#f85149;animation:pulse 1s ease-in-out infinite}

/* ── SCROLLBAR ── */
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track{background:#0d1117}
::-webkit-scrollbar-thumb{background:#21262d;border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:#30363d}
</style>
</head>
<body>

<div class="header">
  <span class="logo">⬡ ALPHA RIVER</span>
  <span class="badge badge-connecting" id="mode-badge">CONECTANDO</span>
  <div class="spacer"></div>
  <div class="conn-status">
    <span class="conn-dot dot-wait" id="ws-dot"></span>
    <span id="ws-label">WebSocket</span>
  </div>
  <div style="color:#8b949e;font-size:11px" id="last-update">—</div>
</div>

<div class="metrics">
  <div class="metric">
    <span class="metric-label">Portfolio</span>
    <span class="metric-value neutral" id="m-portfolio">—</span>
  </div>
  <div class="metric">
    <span class="metric-label">PnL Acumulado</span>
    <span class="metric-value neutral" id="m-pnl-total">—</span>
  </div>
  <div class="metric">
    <span class="metric-label">Posições</span>
    <span class="metric-value neutral" id="m-positions">—</span>
  </div>
  <div class="metric">
    <span class="metric-label">Win Rate</span>
    <span class="metric-value neutral" id="m-winrate">—</span>
  </div>
  <div class="metric">
    <span class="metric-label">Trades</span>
    <span class="metric-value neutral" id="m-trades">—</span>
  </div>
  <div class="metric">
    <span class="metric-label">Kill Switch</span>
    <span class="metric-value ks-ok" id="m-ks">OK</span>
  </div>
</div>

<div class="layout">

  <!-- LEFT: posições abertas + trades fechados -->
  <div style="display:flex;flex-direction:column;gap:12px">

    <div class="panel">
      <div class="panel-title">
        Posições Abertas
        <span class="panel-count" id="pos-count">0</span>
      </div>
      <div class="tbl-wrap">
        <table id="pos-table">
          <thead>
            <tr>
              <th>Símbolo</th>
              <th>T</th>
              <th>Entrada</th>
              <th>Atual</th>
              <th>SL</th>
              <th>PnL%</th>
              <th>PnL USDT</th>
              <th>Trail</th>
              <th>Candles</th>
              <th>Score</th>
            </tr>
          </thead>
          <tbody id="pos-tbody">
            <tr class="empty-row"><td colspan="10">Sem posições abertas</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="panel">
      <div class="panel-title">
        Trades Fechados (últimos 50)
        <span class="panel-count" id="trades-count">0</span>
      </div>
      <div class="trade-log" id="trade-log">
        <div style="text-align:center;color:#484f58;padding:20px;font-style:italic;font-size:12px">Nenhum trade ainda</div>
      </div>
    </div>

  </div>

  <!-- RIGHT: estatísticas -->
  <div class="side-stack">

    <div class="panel">
      <div class="panel-title">Estatísticas</div>
      <div class="stat-grid">
        <div class="stat-cell"><div class="stat-label">Total Trades</div><div class="stat-value neutral" id="s-total">0</div></div>
        <div class="stat-cell"><div class="stat-label">Win Rate</div><div class="stat-value neutral" id="s-wr">0%</div></div>
        <div class="stat-cell"><div class="stat-label">PnL Médio</div><div class="stat-value neutral" id="s-avg">0.00</div></div>
        <div class="stat-cell"><div class="stat-label">PnL Total</div><div class="stat-value neutral" id="s-total-pnl">0.00</div></div>
        <div class="stat-cell"><div class="stat-label">Melhor Trade</div><div class="stat-value pos" id="s-best">—</div></div>
        <div class="stat-cell"><div class="stat-label">Pior Trade</div><div class="stat-value neg" id="s-worst">—</div></div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-title">Distribuição de Saída</div>
      <div style="padding:12px 14px;display:flex;flex-direction:column;gap:8px" id="exit-dist">
        <div style="color:#484f58;font-size:12px;font-style:italic">Sem dados</div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-title">Engine</div>
      <div style="padding:12px 14px;display:flex;flex-direction:column;gap:6px;font-size:12px" id="engine-info">
        <div style="display:flex;justify-content:space-between"><span style="color:#8b949e">Modo</span><span id="ei-mode">—</span></div>
        <div style="display:flex;justify-content:space-between"><span style="color:#8b949e">DB Path</span><span id="ei-db" style="font-size:11px">—</span></div>
        <div style="display:flex;justify-content:space-between"><span style="color:#8b949e">Capital Inicial</span><span id="ei-capital">—</span></div>
        <div style="display:flex;justify-content:space-between"><span style="color:#8b949e">Portfolio</span><span id="ei-portfolio">—</span></div>
      </div>
    </div>

  </div>
</div>

<script>
const fmtUsdt = v => (v >= 0 ? '+' : '') + v.toFixed(2) + ' USDT';
const fmtPct  = v => (v >= 0 ? '+' : '') + (v * 100).toFixed(2) + '%';
const fmtPrice = v => v < 1 ? v.toFixed(5) : v < 100 ? v.toFixed(3) : v.toFixed(2);
const ts2time = ts => ts ? new Date(ts).toLocaleTimeString('pt-BR') : '—';
const clsPnl  = v => v > 0 ? 'pos' : v < 0 ? 'neg' : 'neutral';

const tagReason = r => {
  const m = {SL:'tag-sl',TRAILING:'tag-trailing',MAX_HOLD:'tag-maxhold',KILL_SWITCH:'tag-ks'};
  return `<span class="tag ${m[r]||'tag-sl'}">${r}</span>`;
};
const tagTier = t => `<span class="tag tag-t${t}">T${t}</span>`;

// State
let positions = {};
let prices    = {};
let stats     = {total_trades:0, win_rate:0, avg_pnl_usdt:0, total_pnl_usdt:0, best_pnl:null, worst_pnl:null};
let exitDist  = {};
let recentTrades = [];
let portfolioValue = null;
let initialCapital = null;
let mode = 'DRY_RUN';
let wsConnected = false;

// ── DOM helpers ───────────────────────────────────────────────────────────────
function setMetric(id, val, cls) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = val;
  el.className = 'metric-value ' + (cls || 'neutral');
}

function renderPositions() {
  const tbody = document.getElementById('pos-tbody');
  const pids  = Object.keys(positions);
  document.getElementById('pos-count').textContent = pids.length;

  if (pids.length === 0) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="10">Sem posições abertas</td></tr>';
    return;
  }

  // Ordena por PnL% descrescente
  const sorted = pids.map(pid => {
    const p = positions[pid];
    const cur = prices[p.symbol] || p.entry_price;
    const pnlPct = (cur - p.entry_price) / p.entry_price;
    const pnlUsdt = (cur - p.entry_price) * p.qty;
    return {...p, cur, pnlPct, pnlUsdt};
  }).sort((a, b) => b.pnlPct - a.pnlPct);

  tbody.innerHTML = sorted.map(p => `
    <tr>
      <td><b>${p.symbol.replace('USDT','')}</b><span style="color:#484f58">/USDT</span></td>
      <td>${tagTier(p.tier||1)}</td>
      <td>${fmtPrice(p.entry_price)}</td>
      <td>${fmtPrice(p.cur)}</td>
      <td style="color:#f85149">${fmtPrice(p.sl_price)}</td>
      <td class="${clsPnl(p.pnlPct)}">${fmtPct(p.pnlPct)}</td>
      <td class="${clsPnl(p.pnlUsdt)}">${fmtUsdt(p.pnlUsdt)}</td>
      <td>${p.trailing_active ? '<span class="tag tag-active">✓</span>' : '<span class="tag tag-pending">—</span>'}</td>
      <td style="color:#8b949e">${p.candles_open||0}c</td>
      <td style="color:#8b949e">${p.score ? p.score.toFixed(3) : '—'}</td>
    </tr>
  `).join('');
}

function renderTrades() {
  const log = document.getElementById('trade-log');
  document.getElementById('trades-count').textContent = recentTrades.length;
  if (!recentTrades.length) {
    log.innerHTML = '<div style="text-align:center;color:#484f58;padding:20px;font-style:italic;font-size:12px">Nenhum trade ainda</div>';
    return;
  }
  log.innerHTML = recentTrades.map(t => `
    <div class="trade-item">
      <span class="trade-sym">${t.symbol.replace('USDT','<span style="color:#484f58">/USDT</span>')}</span>
      ${tagReason(t.close_reason||'SL')}
      <span class="trade-pnl ${clsPnl(t.pnl_usdt||0)}">${fmtUsdt(t.pnl_usdt||0)}</span>
      <span class="trade-meta">${fmtPct(t.pnl_pct||0)}<br>${ts2time(t.closed_at)}</span>
    </div>
  `).join('');
}

function renderStats() {
  const pnlTotal = stats.total_pnl_usdt || 0;
  const wr = stats.win_rate || 0;
  const avg = stats.avg_pnl_usdt || 0;

  document.getElementById('s-total').textContent = stats.total_trades || 0;
  setMetric('m-trades', stats.total_trades || 0);

  const wrEl = document.getElementById('s-wr');
  wrEl.textContent = wr.toFixed(1) + '%';
  wrEl.className = 'stat-value ' + (wr >= 50 ? 'pos' : 'neg');
  setMetric('m-winrate', wr.toFixed(1) + '%', wr >= 50 ? 'pos' : 'neg');

  const avgEl = document.getElementById('s-avg');
  avgEl.textContent = (avg >= 0 ? '+' : '') + avg.toFixed(4) + ' USDT';
  avgEl.className = 'stat-value ' + clsPnl(avg);

  const tpEl = document.getElementById('s-total-pnl');
  tpEl.textContent = (pnlTotal >= 0 ? '+' : '') + pnlTotal.toFixed(2) + ' USDT';
  tpEl.className = 'stat-value ' + clsPnl(pnlTotal);
  setMetric('m-pnl-total', (pnlTotal >= 0 ? '+' : '') + pnlTotal.toFixed(2) + ' USDT', clsPnl(pnlTotal));

  if (stats.best_pnl != null) {
    document.getElementById('s-best').textContent = '+' + stats.best_pnl.toFixed(4) + ' USDT';
  }
  if (stats.worst_pnl != null) {
    document.getElementById('s-worst').textContent = stats.worst_pnl.toFixed(4) + ' USDT';
  }
}

function renderExitDist() {
  const cont = document.getElementById('exit-dist');
  const total = Object.values(exitDist).reduce((a, b) => a + b, 0);
  if (!total) return;
  const colors = {SL:'#f85149',TRAILING:'#3fb950',MAX_HOLD:'#58a6ff',KILL_SWITCH:'#e3b341'};
  cont.innerHTML = Object.entries(exitDist).sort((a,b)=>b[1]-a[1]).map(([reason, count]) => {
    const pct = (count / total * 100).toFixed(0);
    const color = colors[reason] || '#8b949e';
    return `
      <div style="display:flex;align-items:center;gap:8px">
        <span style="width:60px;font-size:11px;color:${color}">${reason}</span>
        <div style="flex:1;background:#161b22;border-radius:3px;height:8px">
          <div style="width:${pct}%;background:${color};height:8px;border-radius:3px"></div>
        </div>
        <span style="font-size:11px;color:#8b949e;min-width:28px;text-align:right">${count}</span>
      </div>
    `;
  }).join('');
}

function renderPortfolio() {
  if (portfolioValue === null) return;
  setMetric('m-portfolio', portfolioValue.toFixed(2) + ' USDT');
  setMetric('m-positions', Object.keys(positions).length);
  document.getElementById('ei-portfolio').textContent = portfolioValue.toFixed(2) + ' USDT';
}

// ── Snapshot handler ──────────────────────────────────────────────────────────
function applySnapshot(snap) {
  // Posições
  positions = {};
  (snap.positions || []).forEach(p => { positions[p.position_id] = p; });

  // Preços
  Object.assign(prices, snap.prices || {});

  // Stats
  if (snap.stats) {
    stats = {...stats, ...snap.stats};
    renderStats();
  }
  if (snap.best_pnl !== undefined) stats.best_pnl = snap.best_pnl;
  if (snap.worst_pnl !== undefined) stats.worst_pnl = snap.worst_pnl;

  // Trades recentes
  if (snap.recent_trades) {
    recentTrades = snap.recent_trades;
    // Exit distribution
    exitDist = {};
    recentTrades.forEach(t => {
      const r = t.close_reason || 'SL';
      exitDist[r] = (exitDist[r] || 0) + 1;
    });
    renderTrades();
    renderExitDist();
  }

  // Portfolio
  if (snap.portfolio_value != null) {
    portfolioValue = snap.portfolio_value;
    document.getElementById('ei-portfolio').textContent = portfolioValue.toFixed(2) + ' USDT';
  }
  if (snap.initial_capital != null) {
    initialCapital = snap.initial_capital;
    document.getElementById('ei-capital').textContent = initialCapital.toFixed(2) + ' USDT';
  }
  if (snap.mode) {
    mode = snap.mode;
    const badge = document.getElementById('mode-badge');
    badge.textContent = mode;
    badge.className = 'badge ' + (mode === 'DRY_RUN' ? 'badge-dryrun' : 'badge-live');
  }
  if (snap.db_path) document.getElementById('ei-db').textContent = snap.db_path;
  document.getElementById('ei-mode').textContent = mode;

  renderPositions();
  renderPortfolio();
  renderStats();
}

// ── Event handlers ────────────────────────────────────────────────────────────
function handleEvent(evt) {
  const {type, data} = evt;

  if (type === 'snapshot') {
    applySnapshot(data);

  } else if (type === 'position_opened') {
    positions[data.position_id] = data;
    renderPositions();
    renderPortfolio();

  } else if (type === 'position_closed') {
    delete positions[data.position_id];
    // Adiciona ao topo do log
    recentTrades.unshift(data);
    if (recentTrades.length > 50) recentTrades.pop();
    // Atualiza distribuição
    const r = data.close_reason || 'SL';
    exitDist[r] = (exitDist[r] || 0) + 1;
    // Atualiza stats online
    const pnl = data.pnl_usdt || 0;
    stats.total_trades = (stats.total_trades || 0) + 1;
    if (stats.best_pnl === null || pnl > stats.best_pnl) stats.best_pnl = pnl;
    if (stats.worst_pnl === null || pnl < stats.worst_pnl) stats.worst_pnl = pnl;
    renderTrades();
    renderExitDist();
    renderPositions();
    renderStats();

  } else if (type === 'price_update') {
    Object.assign(prices, data);
    renderPositions();

  } else if (type === 'portfolio_update') {
    if (data.portfolio_value != null) portfolioValue = data.portfolio_value;
    renderPortfolio();
    setMetric('m-positions', data.open_positions ?? Object.keys(positions).length);

  } else if (type === 'kill_switch') {
    const el = document.getElementById('m-ks');
    if (data.active) {
      el.textContent = '⚠ ATIVO';
      el.className = 'metric-value ks-active';
    } else {
      el.textContent = 'OK';
      el.className = 'metric-value ks-ok';
    }
  }

  document.getElementById('last-update').textContent =
    new Date().toLocaleTimeString('pt-BR');
}

// ── WebSocket ─────────────────────────────────────────────────────────────────
function connectWS() {
  const wsUrl = 'ws://' + location.host + '/ws';
  const ws = new WebSocket(wsUrl);
  const dot   = document.getElementById('ws-dot');
  const label = document.getElementById('ws-label');

  ws.onopen = () => {
    wsConnected = true;
    dot.className = 'conn-dot dot-ok';
    label.textContent = 'Conectado';
  };

  ws.onmessage = e => {
    try { handleEvent(JSON.parse(e.data)); }
    catch(err) { console.warn('WS parse error', err); }
  };

  ws.onclose = () => {
    wsConnected = false;
    dot.className = 'conn-dot dot-err';
    label.textContent = 'Reconectando...';
    setTimeout(connectWS, 3000);
  };

  ws.onerror = () => {
    dot.className = 'conn-dot dot-err';
    label.textContent = 'Erro WS';
  };
}

connectWS();
</script>
</body>
</html>"""


# ──────────────────────────────────────────────────────────────────────────────
# SERVER
# ──────────────────────────────────────────────────────────────────────────────

class DashboardServer:
    """
    Servidor web assíncrono para o dashboard de monitoramento.

    Roda no mesmo event loop da engine sem bloquear o pipeline.
    Usa uvicorn em modo "no-lifespan" para integração limpa com asyncio.
    """

    def __init__(
        self,
        config:        dict,
        state_manager: "StateManager",
        event_bus:     "EventBus",
    ):
        cfg_dash = config.get("dashboard", {})
        self._host    = cfg_dash.get("host", "0.0.0.0")
        self._port    = int(cfg_dash.get("port", 8080))
        self._sm      = state_manager
        self._bus     = event_bus
        self._dry_run = config.get("execution", {}).get("dry_run", False)
        self._initial_capital = config.get("risk", {}).get("initial_capital_usdt", 10_000.0)

        cfg_state = config.get("state", {})
        self._db_path = cfg_state.get("db_path", "alpha_river_state.db")

        # Cache de preços mais recentes por símbolo
        self._prices: dict[str, float] = {}

        # Referência à engine (injetada por set_engine())
        self._engine = None

        # Constrói o app FastAPI
        self._app = self._build_app()

        log.info(
            "dashboard_server_configured",
            host=self._host,
            port=self._port,
            mode="DRY_RUN" if self._dry_run else "LIVE",
        )

    def set_engine(self, engine) -> None:
        """Injeta referência à ExecutionEngine para ler portfolio_value ao vivo."""
        self._engine = engine

    def update_price(self, symbol: str, price: float) -> None:
        """Atualiza cache de preços. Chamado pelo candle_fanout no main.py."""
        self._prices[symbol] = price
        self._bus.publish("price_update", {symbol: price})

    # ── FastAPI app ────────────────────────────────────────────────────────────

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="Alpha River Monitor", docs_url=None, redoc_url=None)

        @app.get("/health")
        async def health():
            """Endpoint de healthcheck para o Railway confirmar que o serviço está ativo."""
            return {"status": "ok", "service": "alpha-river-engine-v3.6"}

        @app.get("/", response_class=HTMLResponse)
        async def root():
            return HTMLResponse(content=_DASHBOARD_HTML)

        @app.get("/api/snapshot")
        async def snapshot():
            return await self._build_snapshot()

        @app.websocket("/ws")
        async def websocket_endpoint(ws: WebSocket):
            await ws.accept()
            queue = self._bus.subscribe(maxsize=300)
            log.debug("dashboard_ws_connected", addr=ws.client)

            try:
                # Envia snapshot inicial completo ao conectar
                snap = await self._build_snapshot()
                await ws.send_text(json.dumps({"type": "snapshot", "data": snap}))

                # Streams de eventos + heartbeat a cada 15s
                heartbeat_task = asyncio.create_task(self._heartbeat(queue))

                try:
                    while True:
                        event = await queue.get()
                        await ws.send_text(json.dumps(event, default=str))
                finally:
                    heartbeat_task.cancel()

            except WebSocketDisconnect:
                log.debug("dashboard_ws_disconnected")
            except Exception as exc:
                log.warning("dashboard_ws_error", error=str(exc))
            finally:
                self._bus.unsubscribe(queue)

        return app

    async def _heartbeat(self, queue: asyncio.Queue) -> None:
        """Insere snapshot periódico na fila do subscriber para manter UI atualizada."""
        while True:
            await asyncio.sleep(15)
            snap = await self._build_snapshot()
            try:
                queue.put_nowait({"type": "snapshot", "data": snap})
            except asyncio.QueueFull:
                pass

    async def _build_snapshot(self) -> dict:
        """
        Constrói snapshot completo do estado atual da engine.
        Lê posições do StateManager (cache em memória) e stats do SQLite.
        """
        # Posições abertas (do cache em memória do StateManager)
        open_positions = list(self._sm._open.values())

        positions_payload = []
        for p in open_positions:
            positions_payload.append({
                "position_id":    p.position_id,
                "symbol":         p.symbol,
                "entry_price":    p.entry_price,
                "qty":            p.qty,
                "sl_price":       p.sl_price,
                "peak":           p.peak,
                "candles_open":   p.candles_open,
                "tier":           p.tier,
                "score":          round(p.score, 4),
                "oi_regime":      p.oi_regime,
                "trailing_active": p.trailing_active,
                "opened_at":      p.opened_at,
            })

        # Stats do banco
        stats = await self._sm._db.get_stats()
        recent_trades = await self._sm._db.get_recent_trades(limit=50)

        # Best/worst PnL para o side panel
        best_pnl  = max((t["pnl_usdt"] for t in recent_trades if t["pnl_usdt"]), default=None)
        worst_pnl = min((t["pnl_usdt"] for t in recent_trades if t["pnl_usdt"]), default=None)

        # Portfolio value
        portfolio_value = (
            self._engine.portfolio_value
            if self._engine is not None
            else self._initial_capital
        )

        return {
            "positions":       positions_payload,
            "prices":          dict(self._prices),
            "stats":           stats,
            "recent_trades":   recent_trades,
            "best_pnl":        best_pnl,
            "worst_pnl":       worst_pnl,
            "portfolio_value": round(portfolio_value, 4),
            "initial_capital": self._initial_capital,
            "mode":            "DRY_RUN" if self._dry_run else "LIVE",
            "db_path":         self._db_path,
            "ts":              int(time.time() * 1000),
        }

    # ── Serve ──────────────────────────────────────────────────────────────────

    async def serve(self) -> None:
        """
        Inicia o servidor uvicorn no event loop atual.
        Crie uma asyncio.Task com este método no main.py.
        """
        config = uvicorn.Config(
            app       = self._app,
            host      = self._host,
            port      = self._port,
            log_level = "warning",      # silencia logs HTTP verbosos do uvicorn
            loop      = "none",         # integra no event loop existente
            access_log= False,
        )
        server = uvicorn.Server(config)

        log.info(
            "dashboard_server_starting",
            url=f"http://{self._host}:{self._port}",
        )

        try:
            await server.serve()
        except asyncio.CancelledError:
            pass
        finally:
            log.info("dashboard_server_stopped")
