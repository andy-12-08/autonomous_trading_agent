# Architecture

## 1. System Overview

High-level view of every package and how they connect.

```
┌─────────────────────────────────────────────────────────────────────┐
│                          main.py                                    │
│                    Entry point + Scheduler                          │
└────────────────────────┬────────────────────────────────────────────┘
                         │ builds via
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        bootstrap.py                                 │
│            Instantiates and wires all components                    │
└──┬──────────┬──────────┬──────────┬──────────┬──────────┬──────────┘
   │          │          │          │          │          │
   ▼          ▼          ▼          ▼          ▼          ▼
┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────────┐
│core/ │  │data/ │  │risk/ │  │anal- │  │agent-│  │trading/  │
│      │  │      │  │      │  │ysis/ │  │s/    │  │          │
│Broker│  │7 ext.│  │Risk  │  │Indic-│  │Claude│  │Orchestr- │
│DB    │  │data  │  │GFV   │  │ators │  │Agent │  │ator      │
│      │  │clien-│  │Bucke-│  │Score-│  │Analy-│  │Scanner   │
│      │  │ts    │  │ts    │  │r     │  │st    │  │Executor  │
│      │  │      │  │Expec-│  │Guard │  │      │  │Positions │
│      │  │      │  │tancy │  │      │  │      │  │          │
└──────┘  └──────┘  └──────┘  └──────┘  └──────┘  └──────────┘
```

---

## 2. Startup Flow

```mermaid
flowchart TD
    A([python main.py]) --> B{--dry-run flag?}
    B -- yes --> C[dry_run = True\nno real orders placed]
    B -- no --> D[dry_run = False\nlive paper trading]
    C & D --> E[bootstrap.build_trading_stack]

    E --> F[db.init_db\ncreate SQLite tables]
    F --> G[Instantiate all components\nBroker, Risk, Indicators\nAgents, Data clients...]
    G --> H[orchestrator.reset_daily_state\nclear deployed capital\nreset session counters]
    H --> I[APScheduler starts]

    I --> J[Job 1: run_position_management\nevery 2 minutes]
    I --> K[Job 2: run_scan_and_trade\nevery 10 minutes]
    I --> L[Job 3: run_backtest\nSunday 8 AM only]
    I --> M[Immediate one-shot fires\nboth jobs right now]

    J & K & L & M --> N([Bot running — waiting for next tick])
```

---

## 3. Daily Schedule

```mermaid
flowchart LR
    T1([09:15 ET]) --> A[Morning Study\nClaude reads market\nproduces Daily Plan]
    A --> T2([09:35 ET])
    T2 --> B[Trading window opens\nScan + Trade loop begins]
    B --> C{Every 2 min}
    C --> D[Position Management\nstops / partial profits]
    B --> E{Every 10 min}
    E --> F[Scan + Score + Claude\ndecisions + orders]
    D & F --> T3([15:45 ET])
    T3 --> G[Force-close ALL positions\nNo overnight holds]
    G --> H[Write daily summary\nEmail P and L report]
    H --> T4([Market closed])
    T4 --> I{Sunday?}
    I -- yes --> J[08:00 AM\nWeekly Backtest\n180-day simulation\nEmail strategy report]
    I -- no --> T4
```

---

## 4. Morning Study Flow (09:15–09:34 ET)

Runs once per day. Claude reads everything before a single trade is placed.

```mermaid
flowchart TD
    START([Morning Study triggered]) --> A[market_analyst.run_morning_study]

    A --> B1[study_data._get_market_context\nSPY QQQ UVXY sector ETFs\n5-min bars + indicators]
    A --> B2[study_data._get_economic_calendar\nForexFactory USD events\nFOMC / CPI / NFP detection]
    A --> B3[study_data._get_gap_and_breadth\nSnapshot sweep of watchlist\ngap pct + advance/decline ratio]
    A --> B4[study_data._get_full_history\nAll past decisions from DB\nwins losses setups]
    A --> B5[study_data._get_missed_opportunities\nSkipped setups that moved\ncalibration signal]

    B1 & B2 & B3 & B4 & B5 --> C[broker.get_bars_multi\n5-min bars for all watchlist symbols\ncompute indicators + signal summary]

    C --> D[broker.get_news_headlines\nOvernight news last 18h\ncatalyst context]
    D --> E[pre_market.get_premarket_data\ngap levels + extended hours high/low]
    E --> F[yield_curve.get_yield_curve\nmacro risk posture signal]
    F --> G[short_interest.get_short_interest\nsqueeze candidates]

    G --> H{FOMC day detected?}
    H -- yes --> I[macro_flag = stand_aside\nHARD OVERRIDE injected\ninto prompt]
    H -- no --> J[macro_flag = caution or none]

    I & J --> K[Build structured prompt\nAll data assembled into JSON]
    K --> L[Claude API call\nprompts/morning_study.md]
    L --> M[Parse JSON response\nDaily Trading Plan]

    M --> N[_save_daily_plan to DB]
    N --> O{FOMC override active?}
    O -- yes --> P[Force risk_posture = stand_aside\nregardless of Claude output]
    O -- no --> Q[Use Claude plan as-is]

    P & Q --> END([Plan stored — trading loop may begin])
```

---

## 5. Ten-Minute Scan and Trade Cycle (09:35–15:44 ET)

```mermaid
flowchart TD
    START([Scheduler fires every 10 min]) --> A[run_scan_and_trade]
    A --> B[Spawn daemon thread\n480s timeout watchdog]
    B --> C[_scan_body begins]

    C --> D{Market open?\nCorrect session date?}
    D -- no --> EXIT([Return — skip this tick])
    D -- yes --> E

    E{In study window\n09:15-09:34?} -- yes --> F[run_morning_study\nsee Diagram 4]
    E -- no --> G

    G{Study complete?} -- no --> EXIT2([Return — wait for study])
    G -- yes --> H

    H[market_guard.check_circuit_breaker\ndaily loss too deep?]
    H -- tripped --> EXIT3([Stand aside — circuit breaker active])
    H -- clear --> I

    I[market_guard.get_vix_regime\nVIX level → size multiplier]
    I --> J[market_guard.get_intraday_regime\ntrending / ranging / choppy]
    J --> K[screener.build_universe\nmovers + most-active + watchlist]

    K --> L[ScannerMixin.build_watchlist_data]

    subgraph SCAN [Scanner — one pass per symbol]
        L --> L1[broker.get_bars_multi\n5min + 15min + daily bars\n3 API calls total]
        L1 --> L2[indicators.compute_indicators\nEMA MACD VWAP RSI ATR\nper symbol]
        L2 --> L3[indicators.get_signal_summary\nflat dict of all signals]
        L3 --> L4[PatternsMixin methods\npremium/discount FVG\nliquidity sweep volume profile]
        L4 --> L5[signal_scorer.filter_watchlist\nscore 0-10 drop below threshold]
        L5 --> L6[Enrich survivors only\noptions_flow dark_pool\ninsider_flow edgar]
    end

    L6 --> M[build_positions_snapshot\nmerge broker positions with DB]

    M --> N[trading_agent.ask_agent\nsend to Claude\nprompts/trading_agent.md]

    subgraph CLAUDE [Claude receives]
        N --> N1[scored watchlist candidates]
        N --> N2[open positions + PnL]
        N --> N3[account equity + settled cash]
        N --> N4[Daily Trading Plan + bias]
    end

    N1 & N2 & N3 & N4 --> O[Claude returns JSON\narray of decisions\nBUY SELL HOLD SKIP]

    O --> P[ExecutorMixin.execute_decisions\nsee Diagram 6]
    P --> Q[dynamic_watchlist.save\npre-Claude survivors\nfor tomorrow morning]
    Q --> END([Tick complete — sleep until next 10-min fire])
```

---

## 6. Execution Gauntlet — 7 Gates Before Any Order

Every BUY decision from Claude must pass all seven checks. A single failure skips the trade and records the reason in the DB.

```mermaid
flowchart TD
    START([Claude says BUY symbol X]) --> G1

    G1{Gate 1\nEarnings blackout?}
    G1 -- yes --> SKIP1([SKIP — binary gap risk\nrecord EARNINGS_BLACKOUT])
    G1 -- no --> G2

    G2{Gate 2\nRevenge-trade guard?\nconsecutive losses check}
    G2 -- triggered --> SKIP2([SKIP — emotional state risk\nrecord REVENGE_TRADE])
    G2 -- clear --> G3

    G3{Gate 3\nDynamic confidence bar?\nrecent win rate below 40 pct}
    G3 -- below bar --> SKIP3([SKIP — strategy in drawdown\nrecord DYN_CONFIDENCE])
    G3 -- above bar --> G4

    G4{Gate 4\nSector bucket full?\nmax positions per sector}
    G4 -- full --> SKIP4([SKIP — sector overexposed\nrecord BUCKET_FULL])
    G4 -- clear --> G5

    G5{Gate 5\nCorrelation guard?\nPearson r with open positions}
    G5 -- too correlated --> SKIP5([SKIP — concentrated factor bet\nrecord CORRELATION])
    G5 -- uncorrelated --> G6

    G6{Gate 6\nGFV risk?\ngood-faith violation check}
    G6 -- unsafe --> SKIP6([SKIP — cash settlement rule\nrecord GFV_BLOCK])
    G6 -- safe --> G7

    G7{Gate 7\nATR too high?\nextreme volatility filter}
    G7 -- too volatile --> SKIP7([SKIP — position sizing impossible\nrecord ATR_TOO_HIGH])
    G7 -- ok --> EXEC

    EXEC[risk_manager.compute_stop_take_profit\nATR-based SL and TP\noverrides Claude levels]
    EXEC --> SIZE[risk_manager.compute_position_size\nmax risk per trade dollar cap]
    SIZE --> ORDER{dry_run?}
    ORDER -- yes --> DRY([LOG only — no order sent])
    ORDER -- no --> BRACKET[broker.place_bracket_order\nentry + stop-loss leg + take-profit leg]
    BRACKET --> DB[database.record_decision\ngfv_tracker.add_buy\nnotifier.send_trade_alert]
    DB --> END([Order live])
```

---

## 7. Two-Minute Position Management Loop

Fast path — no Claude involved, no market scanning.

```mermaid
flowchart TD
    START([Scheduler fires every 2 min]) --> A[run_position_management]

    A --> B{Market open?}
    B -- no --> EXIT([Return])
    B -- yes --> C

    C{Session date changed?\nnew trading day}
    C -- yes --> D[reset_daily_state\nclear counters\nreset circuit breaker]
    D --> E
    C -- no --> E

    E{EOD window?\ntime >= 15:45 ET}
    E -- yes --> F[eod_close_all\nclose every open position\nGFV-safe check per symbol]
    F --> G[write_daily_summary\nwin/loss/PnL counts\nexpectancy report]
    G --> H[dynamic_watchlist.save\npersist pre-Claude survivors]
    H --> I[notifier.send_daily_summary\nemail end-of-day report]
    I --> EXIT2([Done for today])

    E -- no --> J[build_positions_snapshot\nmerge broker + DB positions]

    J --> K[check_time_stops\nmax hold time exceeded?]
    K -- yes --> L[broker.close_position\nrecord SELL + outcome]
    K -- no --> M

    M[check_partial_profits\nPnL >= 2R target?]
    M -- yes --> N[broker partial sell 50 pct\nrecord PARTIAL_SELL]
    M -- no --> EXIT3([No action this tick])

    L & N --> EXIT3
```

---

## 8. Data Flow — What Feeds Claude

```mermaid
flowchart LR
    subgraph EXTERNAL [External Sources]
        E1[Alpaca API\nbars quotes snapshots\norders positions]
        E2[ForexFactory\neconomic calendar]
        E3[yfinance\noptions chain\ninsider transactions\nshort interest\nearnings dates]
        E4[Alpaca News API\nheadlines]
        E5[US Treasury API\nyield curve]
        E6[Dark pool feeds]
        E7[SEC EDGAR\n8-K filings]
    end

    subgraph PROCESSED [Processed Signals]
        P1[IndicatorEngine\nEMA9/21/50 MACD VWAP\nRSI ATR vol_ratio]
        P2[PatternsMixin\nFVG liquidity sweeps\npremium/discount\nvolume profile key levels]
        P3[SignalScorer\n0-10 signal score\nsetup classification]
        P4[MarketGuard\nVIX regime\nintraday regime\ncircuit breaker]
        P5[ExpectancyEngine\nrolling win rate\nKelly factor\ncooling symbols]
    end

    subgraph CLAUDE_INPUT [What Claude Sees]
        C1[Scored watchlist\nhigh-conviction setups only]
        C2[Open positions\nentry price PnL stop levels]
        C3[Account state\nequity settled cash deployed]
        C4[Daily Trading Plan\nbias regime macro flag]
        C5[Enrichment signals\noptions flow dark pool\ninsider activity news]
    end

    E1 --> P1 --> P2 --> P3 --> C1
    E2 --> C4
    E3 --> P5 --> C1
    E4 --> C5
    E5 --> C4
    E6 --> C5
    E7 --> C5
    E1 --> C2
    E1 --> C3
    P4 --> C4
    P5 --> C4
```

---

## 9. Risk Layer — How Positions Are Sized and Protected

```mermaid
flowchart TD
    A([Trade approved by all 7 gates]) --> B[Get latest quote\nbid/ask spread]

    B --> C[RiskManager.compute_stop_take_profit\nstop = entry minus 1x ATR\ntake_profit = entry plus 2x ATR\n2:1 reward-to-risk ratio]

    C --> D[RiskManager.compute_position_size\nrisk_dollars divided by stop_distance\ncapped at MAX_RISK_PER_TRADE]

    D --> E[Apply VIX multiplier\nhigh VIX = smaller size\nlow VIX = normal size]

    E --> F[Apply Kelly factor\npoor recent expectancy = reduce size]

    F --> G[Final qty rounded\nto whole shares]

    G --> H{Settled cash\ncovers order?}
    H -- no --> SKIP([SKIP — T+1 cash settlement\ninsufficient settled funds])
    H -- yes --> I[place_bracket_order\n3 legs submitted atomically]

    subgraph BRACKET [Bracket Order Structure]
        I --> I1[Entry leg\nmarket or limit]
        I --> I2[Stop-loss leg\nautomatically cancels if TP hit]
        I --> I3[Take-profit leg\nautomatically cancels if SL hit]
    end

    I1 & I2 & I3 --> J[GFVTracker.add_buy\ntrack unsettled purchase\nprevent good-faith violations]
    J --> K([Position live and protected])
```
