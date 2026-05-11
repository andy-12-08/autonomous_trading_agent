import config
from agents.algo_decisions import AlgoDecisionEngine
from data.news_stream import NewsStream
from agents.algo_analyst import AlgoMarketAnalyst
from agents.dynamic_watchlist import DynamicWatchlist
from analysis.indicators import IndicatorEngine
from analysis.market_guard import MarketGuard
from analysis.screener import Screener
from analysis.signal_scorer import SignalScorer
from core.broker import AlpacaBroker
from core.database import Database
from data.dark_pool import DarkPoolClient
from data.edgar import EdgarClient
from data.insider_flow import InsiderFlowClient
from data.options_flow import OptionsFlowClient
from data.pre_market import PreMarketAnalyzer
from data.short_interest import ShortInterestClient
from data.yield_curve import YieldCurveClient
from risk.bucket_manager import BucketManager
from risk.expectancy import ExpectancyEngine
from risk.gfv_tracker import GFVTracker
from risk.manager import RiskManager
from trading.notifier import Notifier
from trading.orchestrator import TradingOrchestrator
from trading.session_overrides import SessionOverrides
from utils.backtester import Backtester


def build_trading_stack(dry_run: bool = False) -> tuple[TradingOrchestrator, Backtester]:
    """Construct broker, data clients, risk engines, orchestrator, and weekly backtester.

    Starts the optional real-time news stream and attaches it to the broker. Calls
    reset_daily_state on the orchestrator and enables dry-run mode when requested.

    Args:
        dry_run: When True, the orchestrator logs decisions but does not place orders.

    Returns:
        A tuple of the configured TradingOrchestrator and Backtester instances.
    """
    db = Database(config.DB_PATH)
    db.init_db()

    broker = AlpacaBroker()
    risk_manager = RiskManager()
    gfv_tracker = GFVTracker(config.DB_PATH)
    bucket_manager = BucketManager()
    expectancy_engine = ExpectancyEngine(config.DB_PATH)
    options_flow = OptionsFlowClient()
    insider_flow = InsiderFlowClient()
    dark_pool = DarkPoolClient()
    pre_market = PreMarketAnalyzer()
    yield_curve = YieldCurveClient()
    short_interest = ShortInterestClient()
    edgar = EdgarClient()
    indicators = IndicatorEngine()
    signal_scorer = SignalScorer()
    screener = Screener(broker)
    market_guard = MarketGuard(broker, indicators)
    algo_engine = AlgoDecisionEngine()
    dynamic_watchlist = DynamicWatchlist()
    session_overrides = SessionOverrides(config)
    notifier = Notifier(config, config.DB_PATH, expectancy_engine)
    market_analyst = AlgoMarketAnalyst(
        broker, indicators, pre_market, yield_curve, short_interest, dynamic_watchlist
    )
    backtester = Backtester(broker, indicators, signal_scorer)

    orchestrator = TradingOrchestrator(
        broker=broker,
        indicators=indicators,
        risk_manager=risk_manager,
        bucket_manager=bucket_manager,
        gfv_tracker=gfv_tracker,
        signal_scorer=signal_scorer,
        expectancy_engine=expectancy_engine,
        options_flow=options_flow,
        insider_flow=insider_flow,
        dark_pool=dark_pool,
        pre_market=pre_market,
        yield_curve=yield_curve,
        short_interest=short_interest,
        edgar=edgar,
        algo_engine=algo_engine,
        market_analyst=market_analyst,
        market_guard=market_guard,
        notifier=notifier,
        screener=screener,
        dynamic_watchlist=dynamic_watchlist,
        session_overrides=session_overrides,
        database=db,
    )

    news_stream = NewsStream()
    news_stream.start()
    broker._news_stream = news_stream

    orchestrator.reset_daily_state()
    if dry_run:
        orchestrator.set_dry_run(True)

    return orchestrator, backtester
