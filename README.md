# Hyperliquid S/R Trading Bot — Sweep & Reclaim v1.0

Geautomatiseerde tradingbot voor Hyperliquid DEX, gebaseerd op Liquidity Sweep & Reclaim bij Support & Resistance zones. Ontworpen door Claude (Opus) en Krabje (OpenClaw) voor testdoeleinden.

## Setup & Installation

You can run this bot as a standalone testnet trading bot without the full OpenClaw infrastructure.

1. **Clone the repository:**
   ```bash
   git clone https://github.com/doosjenever/hyperliquid-bot.git
   cd hyperliquid-bot
   ```

2. **Create a virtual environment & install dependencies:**
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Configure environment variables:**
   ```bash
   cp .env.example .env
   # Add your Hyperliquid Testnet Wallet and Private Key
   nano .env
   ```

4. **Run the bot:**
   ```bash
   python main.py
   ```

## Strategie

De bot handelt perpetual futures op Hyperliquid door te wachten op **liquidatie-sweeps** bij S/R zones. In plaats van te kopen bij support (zoals retail), wacht de bot tot de zone DOORBROKEN wordt (sweep — triggert retail stop-losses), en koopt pas wanneer de prijs de zone terugverovert (reclaim).

### Sweep & Reclaim Entry Flow

```
1. Identificeer HTF S/R zone (swing highs/lows + volume profile)
2. SWEEP: prijs breekt DOOR zone (low < support voor longs)
   ├── Min diepte: 0.15x ATR (filtert noise)
   └── Max diepte: 1.5x ATR (dieper = trend break, niet sweep)
3. RECLAIM: candle SLUIT terug binnen zone
   └── Prijs-gebonden window (geen tijdslimiet)
4. CVD check: bevestig echte kopers/verkopers
5. Confluence scoring: min 90 punten
6. ENTRY bij reclaim close
7. STOP onder sweep wick - (0.5 * ATR * vol_ratio)
```

### Confluence Scoring (min. 90 punten voor een trade)

| Signaal | Punten | Bron |
|---------|--------|------|
| Sweep + Reclaim bevestigd | +50 | Kern van de strategie |
| Zone strength 3+ touches | +15 | Sterkere zones = betrouwbaarder |
| Zone strength 2 touches | +10 | Multi-touch bevestiging |
| Volume Profile POC | +25 | Volume-gewogen prijsniveau |
| High Volume Node (HVN) | +20 | Volume nodes > 1.5x gemiddelde |
| CVD bevestiging | +20 | Netto koop/verkoopdruk |
| Extreme funding (gunstig) | +30 | Crowd zit fout = edge |
| RSI extreme | +10 | Oversold/overbought per asset percentiel |
| EMA Trend Alignment | +10/-30 | Counter-trend longs: -30 (falling knife) |
| Extreme funding (tegen) | -40 | Hard veto op crowd-trades |

### Waarom Sweep & Reclaim?

```
Oude strategie (retail):        Nieuwe strategie (Smart Money):

S/R Zone ─────────              S/R Zone ─────────
  ↓ Prijs raakt zone              ↓ Prijs BREEKT door zone
  → Entry (te vroeg)                ↓ Stop-losses getriggerd (sweep)
  ↓ Prijs breekt door               ↓ Prijs komt TERUG in zone (reclaim)
  → Stop-loss hit (verlies)          → Entry hier (na bevestiging)
                                     → Stop onder sweep wick
```

### Exit Strategie

- **Stop-loss**: sweep wick - (0.5 * ATR * vol_ratio) — dynamisch
- **Take-profit**: Fair Value Gap (als beschikbaar), anders 2R fallback
- **Trail-to-BE**: stop naar break-even na 1R winst
- **RSI exit**: winst nemen bij extreme RSI (>75 long, <25 short)
- **Single entry**: 100% positie, geen DCA (sweep reclaim = binary thesis)

### Range Filter (Kaufman Efficiency Ratio)

BTC verloor consistent in choppy ranges (66-71k band). Oplossing: Kaufman Efficiency Ratio als range-detectie filter.

```
Efficiency Ratio = |net move over 20 candles| / sum(|individuele moves|)
  ER = 0  ->  pure chop (markt beweegt, gaat nergens heen)
  ER = 1  ->  pure trend (elke candle dezelfde richting)

Threshold per asset (scaled by 1/vol_ratio^2):
  BTC:  0.20 / 1.00^2 = 0.200  (streng - filtert choppy ranges)
  ETH:  0.20 / 1.33^2 = 0.113  (relaxed)
  SOL:  0.20 / 1.44^2 = 0.096  (relaxed)
  DOGE: 0.20 / 1.46^2 = 0.094  (relaxed)
```

### Backtest Resultaten (90 dagen, Sweep & Reclaim v1.0 + Range Filter)

| Asset | Leverage | Trades | PnL | PF | Win% | Max DD |
|-------|----------|--------|-----|-----|------|--------|
| BTC | 40x | 9 | -0.8% | 0.92 | 44% | 4.4% |
| ETH | 25x | 10 | +2.8% | 1.30 | 50% | 4.4% |
| SOL | 20x | 11 | +4.2% | 1.47 | 45% | 2.3% |
| DOGE | 10x | 7 | +3.0% | 1.63 | 57% | 4.3% |
| **Totaal** | | **37** | **+9.1%** | | **49%** | |

**BTC verbeterd van -8.5% (PF 0.53) naar -0.8% (PF 0.92) dankzij range filter**
**Portfolio verdubbeld van +4.6% naar +9.1%**

### Adaptief Asset Profiel

Elke asset krijgt automatisch gekalibreerde parameters:

```
AssetProfile (BTC als baseline):
┌──────────┬───────────┬──────────────────────┐
│ Asset    │ Vol Ratio │ Stop Buffer          │
├──────────┼───────────┼──────────────────────┤
│ BTC      │ 1.00x     │ 0.50 ATR             │
│ ETH      │ 1.33x     │ 0.67 ATR             │
│ SOL      │ 1.44x     │ 0.72 ATR             │
│ DOGE     │ 1.46x     │ 0.73 ATR             │
└──────────┴───────────┴──────────────────────┘
Stop buffer = 0.5 * ATR * vol_ratio
```

## Architectuur

### Twee-lagen model

```
┌─────────────────────────────────────────────────┐
│  LAAG 1: Fast Path (standalone async Python)    │
│                                                 │
│  WebSocket ──► L2+Trades  ──► Sweep Engine      │
│  (tick data)   (CVD calc)     (zone monitoring)  │
│                                    │            │
│                              State Machine      │
│                    (IDLE → SWEEP → RECLAIM →    │
│                     IN_POSITION → EXIT)         │
│                                    │            │
│                              SDK ──► Hyperliquid│
│                           (EIP-712 signing)     │
└─────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────┐
│  LAAG 2: Smart Path (OpenClaw / Krabje)         │
│                                                 │
│  MCP Server ──► Hyperliquid (read-only/control) │
│       │                                         │
│  Krabje ──► Telegram (PnL, alerts, kill switch) │
│       │                                         │
│  SKILL.md ──► "Hoe staat de bot ervoor?"        │
└─────────────────────────────────────────────────┘
```

## Risk Management

| Maatregel | Implementatie |
|-----------|---------------|
| Position Sizing | 2% account equity risico per trade |
| Max Margin Usage | 75% — geen nieuwe trades boven grens |
| Circuit Breaker | >5% equity drop in 15 min → cancel all, pauze 24u |
| Stop-Loss | Sweep wick + dynamische buffer (vol_ratio scaled) |
| Min Stop Distance | 0.5 ATR — voorkomt micro-stops |
| Exit Backoff | Exponential 2s-30s, na 5 fails → HALTED |
| Stale Data Check | WebSocket >3s geen data → pauzeer trading |
| PnL Booking | Pas NA succesvolle exchange close |

## State Machine (9 states)

```
IDLE → EVALUATING → SWEEP_DETECTED → RECLAIM_PENDING → IN_POSITION → EXIT_PENDING → COOLDOWN → IDLE

Speciale states:
  SYNCING    ← WebSocket reconnect → herbevestig positie op exchange
  HALTED     ← 5x exit fail of circuit breaker → handmatige interventie nodig
```

## Projectstructuur

```
hyperliquid-bot/
├── config.py                  # API keys, risk parameters, sweep thresholds
├── run_backtest.py            # Single asset backtest
├── run_backtest_all.py        # Multi-asset backtest
├── run_live.py                # Live bot entry point
├── recalibrate.py             # Wekelijkse parameter recalibratie
├── analyze_market.py           # Krabje CRO: multi-TF marktanalyse (15m cron)
├── cli.py                     # CLI tool (status/kill/positions/trades)
├── data/
│   ├── fetcher.py             # OHLCV data ophalen + parquet cache
│   ├── universe.py            # Dynamische top N assets op Open Interest
│   └── cache/                 # Parquet bestanden
├── strategy/
│   ├── support_resistance.py  # S/R zones (swing highs/lows)
│   ├── volume_profile.py      # Volume Profile (POC, VA, HVN)
│   ├── confluence.py          # Confluence scoring (sweep-centric)
│   ├── sweep_reclaim.py       # Sweep detectie, reclaim, CVD, FVG
│   ├── dca.py                 # SweepPosition (single entry, wick-based stop)
│   └── asset_profile.py       # Adaptief asset profiel (auto-kalibratie)
├── backtest/
│   ├── engine.py              # Backtester (Sweep & Reclaim v1.0)
│   └── slippage.py            # Slippage en fee model
├── execution/
│   ├── fsm.py                 # Per-asset FSM (9 states, sweep-centric)
│   ├── orders.py              # OrderExecutor (Hyperliquid SDK)
│   ├── websocket.py           # WebSocket multiplexer (L2+trades)
│   └── manager.py             # Portfolio Manager (circuit breaker, rotation)
├── notify/
│   └── telegram.py            # PnL alerts, kill switch
└── state/
    ├── database.py            # SQLite module
    └── trades.db              # SQLite database
```

## Bouwfases

### Fase 1-4: Foundation (COMPLEET)
- S/R zones, Volume Profile, confluence scoring
- Per-asset FSM, WebSocket, REST polling
- OrderExecutor, unified account, partial fills
- VPS deployment, dynamische universe, Krabje monitoring

### Fase 5: Sweep & Reclaim Strategy (COMPLEET — 2026-03-29)
- **Liquidity Sweep & Reclaim** als kern-entry (vervangt Mode A/B)
- **CVD bevestiging** (candle proxy in backtest, WebSocket live)
- **Fair Value Gap** take-profit targets
- **Zone strength** als scoring bonus
- **Stronger trend filter**: -30 penalty voor counter-trend longs
- **Kaufman Efficiency Ratio range filter** (BTC chop fix, vol_ratio² schaling)
- **Backtester-Live pariteit** behouden

### Fase 6: Validatie & Mainnet
- BTC strategie optimalisatie (range-detectie of leverage reductie)
- Min. 2 weken testnet validatie
- Walk-forward testing implementeren
- OI divergentie filter (live, via REST polling)
- Mainnet deployment na validatie

### Fase 7: HIP-3 / TradFi Uitbreiding
- PAXG als goud-proxy
- Trade[XYZ] API voor echte TradFi
- Telegram bot integratie

## Vereisten

- Python 3.10+
- `hyperliquid-python-sdk`
- `pandas`, `numpy` (backtesting)
- `websockets`, `asyncio` (live trading)
- Hyperliquid testnet account

## Ontwikkeld door

- **Claude** (Opus) — architectuur, code, strategie-research
- **Krabje** (OpenClaw / Gemini) — strategie-analyse, risk review, monitoring
- **Beheerder** — opdrachtgever, strategie richting
