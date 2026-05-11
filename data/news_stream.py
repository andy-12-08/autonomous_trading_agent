import json
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import websocket

import config
from core.database import log


class NewsStream:
    """Real-time news feed via Alpaca's WebSocket.

    Subscribes to all news events and caches them by symbol in memory.
    Runs in a daemon thread — starts once at bot startup, reconnects automatically.
    """

    WS_URL      = "wss://stream.data.alpaca.markets/v1beta1/news"
    RECONNECT_S = 30    # seconds between reconnect attempts

    def __init__(self):
        self._cache: dict[str, deque] = defaultdict(lambda: deque(maxlen=20))
        self._lock      = threading.Lock()
        self._ws        = None
        self._thread    = None
        self._running   = False
        self._connected = False

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background WebSocket thread. Safe to call multiple times."""
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True, name="news-stream")
        self._thread.start()
        log.info("NewsStream: background thread started")

    def stop(self) -> None:
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def get_news(self, symbols: list[str], max_age_minutes: int = 30) -> dict[str, list[dict]]:
        """Return recent cached news for the given symbols.

        Args:
            symbols: Tickers to look up.
            max_age_minutes: Discard articles older than this.

        Returns:
            {symbol: [article, ...]} — only symbols with fresh articles are included.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
        result: dict[str, list[dict]] = {}
        with self._lock:
            for sym in symbols:
                fresh = []
                for article in self._cache.get(sym.upper(), []):
                    try:
                        ts_str = article["created_at"].replace("Z", "+00:00")
                        if datetime.fromisoformat(ts_str) >= cutoff:
                            fresh.append(article)
                    except Exception:
                        fresh.append(article)  # keep on parse failure
                if fresh:
                    result[sym.upper()] = fresh
        return result

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── Internal WebSocket handlers ───────────────────────────────────────────

    def _loop(self) -> None:
        while self._running:
            try:
                self._ws = websocket.WebSocketApp(
                    self.WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=60, ping_timeout=20)
            except Exception as exc:
                log.warning("NewsStream loop error: %s", exc)
            self._connected = False
            if self._running:
                log.info("NewsStream: reconnecting in %ds", self.RECONNECT_S)
                time.sleep(self.RECONNECT_S)

    def _on_open(self, ws) -> None:
        ws.send(json.dumps({
            "action": "auth",
            "key":    config.ALPACA_KEY    or "",
            "secret": config.ALPACA_SECRET or "",
        }))

    def _on_message(self, ws, message: str) -> None:
        try:
            events = json.loads(message)
        except Exception:
            return

        for msg in events:
            T = msg.get("T")

            if T == "success":
                if msg.get("msg") == "authenticated":
                    ws.send(json.dumps({"action": "subscribe", "news": ["*"]}))
                elif msg.get("msg") == "connected":
                    log.info("NewsStream: connected to Alpaca news feed")
                elif msg.get("msg") == "subscribed":
                    self._connected = True
                    log.info("NewsStream: subscribed to all news")

            elif T == "n":  # news article
                self._handle_news(msg)

            elif T == "error":
                log.warning("NewsStream server error: %s", msg.get("msg"))

    def _handle_news(self, msg: dict) -> None:
        article = {
            "headline":   msg.get("headline", ""),
            "summary":    (msg.get("summary") or "")[:200],
            "created_at": msg.get("created_at", datetime.now(timezone.utc).isoformat()),
        }
        symbols = [s.upper() for s in (msg.get("symbols") or [])]
        if article["headline"] and symbols:
            log.info("NewsStream: [%s] %s", ", ".join(symbols[:5]), article["headline"][:80])
            with self._lock:
                for sym in symbols:
                    self._cache[sym].appendleft(article)

    def _on_error(self, ws, error) -> None:
        log.warning("NewsStream error: %s", error)
        self._connected = False

    def _on_close(self, ws, code, msg) -> None:
        self._connected = False
        log.info("NewsStream: connection closed (code=%s)", code)
