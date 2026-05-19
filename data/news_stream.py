import json
import threading
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import websocket

import config
from core.database import log


class NewsStream:
    """Real-time news feed via Alpaca's WebSocket.

    Subscribes to all news events and caches them by symbol in memory.
    Runs in a daemon thread  starts once at bot startup, reconnects automatically.
    """

    WS_URL           = "wss://stream.data.alpaca.markets/v1beta1/news"
    RECONNECT_S      = 30   # normal backoff between reconnect attempts
    LIMIT_BACKOFF_S  = 90   # longer wait when Alpaca reports connection limit exceeded

    def __init__(self):
        """Initialize thread, WebSocket, and in-memory article cache state.

        Returns:
            None.
        """
        self._cache: dict[str, deque] = defaultdict(lambda: deque(maxlen=20))
        self._lock         = threading.Lock()
        self._ws           = None
        self._thread       = None
        self._running      = False
        self._connected    = False
        self._stop_event   = threading.Event()
        self._limit_hit    = False   # True when server says connection limit exceeded

    # -- Public API ------------------------------------------------------------

    def start(self) -> None:
        """Start the background WebSocket thread. Safe to call multiple times."""
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread  = threading.Thread(target=self._loop, daemon=True, name="news-stream")
        self._thread.start()
        log.info("NewsStream: background thread started")

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the background thread to stop and wait for it to exit.

        Sends a proper WebSocket close frame so Alpaca releases the connection
        server-side before the process exits, preventing 'connection limit
        exceeded' on the next startup.

        Args:
            timeout: Seconds to wait for the thread to finish (default 5).
        """
        self._running = False
        self._stop_event.set()
        ws = self._ws
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def get_news(self, symbols: list[str], max_age_minutes: int = 30) -> dict[str, list[dict]]:
        """Return recent cached news for the given symbols.

        Args:
            symbols: Tickers to look up.
            max_age_minutes: Discard articles older than this.

        Returns:
            {symbol: [article, ...]}  only symbols with fresh articles are included.
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
        """Return whether the latest WebSocket subscription handshake completed.

        Returns:
            True when subscribed to the Alpaca news stream.
        """
        return self._connected

    # -- Internal WebSocket handlers -------------------------------------------

    def _loop(self) -> None:
        """Run the reconnecting WebSocket loop until stop() is called.

        Returns:
            None.
        """
        while self._running:
            self._limit_hit = False
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
                # Use a longer backoff when Alpaca says the connection limit was
                # exceeded  the previous process's connection is still alive on
                # the server side and needs time to expire before we can reconnect.
                wait = self.LIMIT_BACKOFF_S if self._limit_hit else self.RECONNECT_S
                log.info("NewsStream: reconnecting in %ds", wait)
                self._stop_event.wait(timeout=wait)

    def _on_open(self, ws) -> None:
        """Authenticate immediately after the WebSocket opens.

        Args:
            ws: websocket-client WebSocketApp instance.

        Returns:
            None.
        """
        ws.send(json.dumps({
            "action": "auth",
            "key":    config.ALPACA_KEY    or "",
            "secret": config.ALPACA_SECRET or "",
        }))

    def _on_message(self, ws, message: str) -> None:
        """Handle Alpaca WebSocket success, news, and error messages.

        Args:
            ws: websocket-client WebSocketApp instance.
            message: Raw JSON message payload from Alpaca.

        Returns:
            None.
        """
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
                err_msg = msg.get("msg", "")
                log.warning("NewsStream server error: %s", err_msg)
                if "connection limit" in str(err_msg).lower():
                    self._limit_hit = True
                    log.warning("NewsStream: connection limit hit  will wait %ds before retry "
                                "to let previous connection expire on Alpaca's side",
                                self.LIMIT_BACKOFF_S)

    def _handle_news(self, msg: dict) -> None:
        """Cache one Alpaca news event under each associated symbol.

        Args:
            msg: Parsed Alpaca news message.

        Returns:
            None.
        """
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
        """Mark the stream disconnected after a WebSocket error.

        Args:
            ws: websocket-client WebSocketApp instance.
            error: Error object or message from websocket-client.

        Returns:
            None.
        """
        log.warning("NewsStream error: %s", error)
        self._connected = False

    def _on_close(self, ws, code, msg) -> None:
        """Mark the stream disconnected after a WebSocket close frame.

        Args:
            ws: websocket-client WebSocketApp instance.
            code: Close status code.
            msg: Close reason text.

        Returns:
            None.
        """
        self._connected = False
        log.info("NewsStream: connection closed (code=%s)", code)
