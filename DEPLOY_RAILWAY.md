# 🚀 Alpha River Engine — Deploy Railway (Guia Completo)

## Pré-requisitos
- Conta Railway ativa com plano Hobby ($5/mês)
- Conta GitHub com repositório privado (a ser criado abaixo)
- Git instalado no seu Windows

---

## Passo 1 — Criar o Repositório GitHub

1. Acesse [github.com/new](https://github.com/new)
2. Nome sugerido: `alpha-river-engine`
3. Marque como **Private**
4. **NÃO** marque "Add a README file" (o repositório precisa estar vazio)
5. Clique em **Create repository**
6. Copie a URL HTTPS exibida (ex: `https://github.com/seu-usuario/alpha-river-engine.git`)

---

## Passo 2 — Inicializar o Git e Fazer o Primeiro Commit

Abra o PowerShell e execute os comandos abaixo **na ordem**:

```powershell
# Navegue até a pasta da versão Railway
cd "C:\Users\Rahul\Pine Script v6 + Crypto Quant Trading\alpha-river-engine-railway"

# Inicializa o repositório git local
git init

# Adiciona todos os arquivos (os bloqueados pelo .gitignore são ignorados automaticamente)
git add .

# Confirma o que está sendo adicionado — revise esta lista antes de continuar!
git status

# Cria o primeiro commit
git commit -m "feat: alpha river engine - railway deploy ready"

# Conecta ao repositório GitHub (substitua pela sua URL real)
git remote add origin https://github.com/SEU-USUARIO/alpha-river-engine.git

# Envia o código
git push -u origin main
```

> ⚠️ **Verifique o `git status`** antes do commit. NÃO devem aparecer arquivos `.db`, `.env` ou `teste1.txt`. Se aparecerem, algo está errado no `.gitignore`.

---

## Passo 3 — Criar o Projeto no Railway

1. Acesse [railway.app](https://railway.app) e faça login
2. Clique em **New Project**
3. Selecione **Deploy from GitHub repo**
4. Autorize o Railway a acessar seu GitHub (se ainda não autorizou)
5. Selecione o repositório `alpha-river-engine`
6. O Railway vai detectar o `railway.toml` automaticamente

---

## Passo 4 — Criar o Volume Persistente (OBRIGATÓRIO)

O Volume garante que o banco SQLite sobreviva a restarts e deploys.

1. No painel do projeto Railway, clique no serviço criado
2. Vá em **Settings → Volumes**
3. Clique em **Add Volume**
4. Configure:
   - **Mount Path**: `/data`
   - **Size**: 1 GB (suficiente para anos de operação)
5. Clique em **Create**

---

## Passo 5 — Configurar Variáveis de Ambiente

No painel do serviço Railway, vá em **Variables** e adicione:

| Variável | Valor | Obrigatório |
|---|---|---|
| `CONFIG` | `config/mainnet_dryrun.yaml` | ✅ Sim |
| `DB_PATH` | `/data/alpha_river_dryrun.db` | ✅ Sim |
| `PYTHONUNBUFFERED` | `1` | ✅ Sim (logs em tempo real) |
| `TZ` | `America/Sao_Paulo` | Recomendado |

> ℹ️ Em modo dry-run, **não são necessárias** as variáveis `BINANCE_TESTNET_API_KEY` ou `BINANCE_TESTNET_API_SECRET`.

---

## Passo 6 — Acionar o Deploy

1. Após configurar o Volume e as variáveis, clique em **Deploy** (ou o Railway inicia automaticamente)
2. Acompanhe os logs em tempo real no painel
3. O processo de inicialização leva ~2-5 minutos (warm-up histórico de 700 candles)

### O que você verá nos logs durante o startup:
```
alpha_river_engine_starting   version=1.2.0-dryrun-dashboard  mode=DRY_RUN (mainnet data)
phase_1_warmup_starting
phase_1_complete              symbols_in_history=NNN
phase_2_execution_engine_init dry_run=True
phase_3_state_manager_init
phase_3_recovery_complete     positions_recovered=0
phase_4_streaming_starting
dashboard_active              url=http://0.0.0.0:PORT
pipeline_active               tasks=[candle_fanout, feature_engine, signal_engine, ...]
```

---

## Passo 7 — Acessar o Dashboard

1. No painel Railway, vá em **Settings → Networking → Generate Domain**
2. Clique em **Generate** para obter seu URL público (ex: `alpha-river-xxxxx.up.railway.app`)
3. Acesse o URL no browser — o Dashboard mostra posições em tempo real

---

## Atualizações de Código Futuras

Para subir uma nova versão do robô:

```powershell
cd "C:\Users\Rahul\Pine Script v6 + Crypto Quant Trading\alpha-river-engine-railway"
git add .
git commit -m "fix: descrição da mudança"
git push
```

O Railway detecta o push automaticamente e faz o deploy da nova versão. O Volume com o banco SQLite é preservado entre deploys.

---

## Troubleshooting

| Sintoma | Causa Provável | Solução |
|---|---|---|
| Build falha com "module not found" | Dependência ausente | Verificar `requirements.txt` |
| Healthcheck timeout | Port mismatch | O Dashboard já lê `$PORT` automaticamente |
| `sqlite3.OperationalError: unable to open` | Volume não montado | Verifique se `/data` foi criado e montado |
| Engine para após 2-3 min | Kill Switch ativado | Normal — BTC.D caiu muito; o engine reinicia o cooldown |
| Logs parados no warm-up | Rate limit Binance | Aguarde ~5 min; o semáforo de 50 req/s é conservador |
