"""
Dependency wiring: constructs every component and assembles the TradingOrchestrator.

Component dependency graph (simplified):
  Database ← everything
  AlpacaBroker (+ OptionsOrdersMixin) ← executor, positions, scanner
  IVAnalyzer ← OptionsDecisionEngine, executor, positions
  GreeksEngine ← executor, positions (stateless — passed as a class reference)
  OptionsStrategySelector ← OptionsDecisionEngine
  OptionsDecisionEngine ← TradingOrchestrator
  OptionsRiskManager ← executor (stateless — passed as a class reference)
  MarketGuard ← orchestrator, trade cycle
  OptionsFlowClient ← trade cycle enrichment, IVAnalyzer
  TradingOrchestrator ← main.py scheduler
"""

import config
from algo.algo_analyst import AlgoMarketAnalyst
from algo.algo_decisions import OptionsDecisionEngine
from algo.dynamic_watchlist import DynamicWatchlist
from analysis.greeks_engine import GreeksEngine
from analysis.indicators import IndicatorEngine
from analysis.market_guard import MarketGuard
from analysis.options_strategy_selector import OptionsStrategySelector
from analysis.screener import Screener
from analysis.signal_scorer import SignalScorer
from core.broker import AlpacaBroker
from core.database import Database
from data.dark_pool import DarkPoolClient
from data.edgar import EdgarClient
from data.iv_analyzer import IVAnalyzer
from data.options_flow import OptionsFlowClient
from data.pre_market import PreMarketAnalyzer
from data.yield_curve import YieldCurveClient
from risk.options_risk import OptionsRiskManager
from trading.notifier import Notifier
from trading.orchestrator import TradingOrchestrator


def build_trading_stack(dry_run: bool = False) -> TradingOrchestrator:
    """
    Construct and wire every component of the options trading stack.

    Args:
        dry_run: When True, the orchestrator logs decisions but places no real orders.

    Returns:
        A fully configured TradingOrchestrator ready to be attached to the scheduler.
    """
    # ── Persistence ───────────────────────────────────────────────────────────
    db = Database(config.DB_PATH)
    db.init_db()

    # ── Broker (also OptionsOrdersMixin via broker.py) ────────────────────────
    broker = AlpacaBroker()

    # ── Market data and analysis ───────────────────────────────────────────────
    indicators        = IndicatorEngine()
    signal_scorer     = SignalScorer()
    screener          = Screener(broker)
    market_guard      = MarketGuard(broker, indicators)

    # ── Options-specific components ────────────────────────────────────────────
    options_flow      = OptionsFlowClient()
    iv_analyzer       = IVAnalyzer()
    greeks_engine     = GreeksEngine()          # stateless; used via class methods
    strategy_selector = OptionsStrategySelector()
    options_risk      = OptionsRiskManager()    # stateless; used via static methods
    algo_engine       = OptionsDecisionEngine()

    # ── Alt-data and macro ─────────────────────────────────────────────────────
    dark_pool         = DarkPoolClient()
    pre_market        = PreMarketAnalyzer()
    yield_curve       = YieldCurveClient()
    edgar             = EdgarClient()

    # ── Session utilities ──────────────────────────────────────────────────────
    dynamic_watchlist = DynamicWatchlist()
    notifier          = Notifier(config, config.DB_PATH)

    # ── Morning study ──────────────────────────────────────────────────────────
    market_analyst    = AlgoMarketAnalyst(
        broker, indicators, pre_market, yield_curve, None, dynamic_watchlist
    )

    # ── Orchestrator ───────────────────────────────────────────────────────────
    orchestrator = TradingOrchestrator(
        broker            = broker,
        iv_analyzer       = iv_analyzer,
        options_flow      = options_flow,
        options_risk      = options_risk,
        greeks_engine     = greeks_engine,
        strategy_selector = strategy_selector,
        algo_engine       = algo_engine,
        market_guard      = market_guard,
        market_analyst    = market_analyst,
        screener          = screener,
        dark_pool         = dark_pool,
        pre_market        = pre_market,
        yield_curve       = yield_curve,
        edgar             = edgar,
        notifier          = notifier,
        database          = db,
        dynamic_watchlist = dynamic_watchlist,
        signal_scorer     = signal_scorer,
    )

    orchestrator.reset_daily_state()

    if dry_run:
        orchestrator.set_dry_run(True)

    return orchestrator
