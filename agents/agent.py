import json
import pathlib
import anthropic
import config
from core.database import log


class TradingAgent:
    """Anthropic client that turns scan context JSON into trade decision rows."""
    SYSTEM_PROMPT = (
        pathlib.Path(__file__).parent.parent / "prompts" / "trading_agent.md"
    ).read_text()

    def __init__(self):
        """Build the Anthropic client using API keys and retry settings from config."""
        self._client = anthropic.Anthropic(
            api_key=config.ANTHROPIC_API_KEY,
            timeout=120.0,
            max_retries=config.CLAUDE_MAX_RETRIES,
        )

    def ask_agent(
        self,
        watchlist_data: list[dict],
        open_positions: list[dict],
        account: dict,
        recent_decisions: list[dict],
        daily_plan: dict | None = None,
        bucket_report: dict | None = None,
    ) -> list[dict] | None:
        """Send watchlist, positions, account state, and history to Claude as JSON.

        Args:
            watchlist_data: Candidate dicts after mechanical screening.
            open_positions: Snapshot rows for each live position.
            account: Equity, cash, limits, and regime strings for the prompt.
            recent_decisions: Prior DB rows for continuity (last 20 are embedded).
            daily_plan: Morning study JSON, or None when unavailable.
            bucket_report: Sector bucket exposure summary dict, or None.

        Returns:
            Parsed list of decision dicts (may be [] for intentional "no action"),
            or None on JSON parse errors or transport failures (triggers fallback).
        """
        history_text = json.dumps(recent_decisions[-20:], indent=2) if recent_decisions else "[]"
        plan_text    = json.dumps(daily_plan, indent=2) if daily_plan else "NOT AVAILABLE — trade conservatively"
        bucket_text  = json.dumps(bucket_report, indent=2) if bucket_report else "{}"

        user_content = f"""## DAILY TRADING PLAN (from morning study — your strategic anchor for today)
{plan_text}

## ACCOUNT STATE
{json.dumps(account, indent=2)}

## OPEN POSITION SECTOR EXPOSURE (actual open positions only — for context)
{bucket_text}

## OPEN POSITIONS — review each for HOLD / SELL / UPDATE_STOP / PARTIAL_SELL
{json.dumps(open_positions, indent=2)}

## WATCHLIST SCAN — evaluate each for BUY or SKIP (cross-reference daily plan candidates)
{json.dumps(watchlist_data, indent=2)}

## RECENT DECISION HISTORY — apply history lessons, avoid repeated losing setups
{history_text}

REMINDER — CASH ACCOUNT GFV RULES:
- Only use SETTLED cash. Never use same-day sale proceeds to fund a new buy AND then sell it same day.
- Each new buy is tagged GFV-safe only if funded from overnight-settled funds.
- Daily capital cap: $4,000. Deployment today so far: ${account.get('deployed_today', 0):.0f}.

Apply all 20 rules and hard constraints. Evaluate each candidate independently on its own setup quality — sector diversification is already enforced by the position manager before and after your decision. Do NOT skip a candidate just because another candidate is in the same sector.

⚠ OUTPUT FORMAT IS STRICT: respond with ONLY a raw JSON array — no preamble, no reasoning,
no markdown, no explanation. Your entire response must start with [ and end with ].
Keep reason_for_entry under 30 words — one tight sentence stating the edge and the risk.
Example of the only acceptable format: [{{"symbol":"AAPL","action":"SKIP",...}}]
Return your JSON decision array now."""

        raw = ""
        try:
            resp = self._client.messages.create(
                model=config.CLAUDE_MODEL,
                max_tokens=16384,
                system=TradingAgent.SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            )
            from anthropic.types import TextBlock
            raw = next((b.text for b in resp.content if isinstance(b, TextBlock)), "").strip()

            if raw.startswith("```"):
                parts = raw.split("```")
                raw = parts[1] if len(parts) > 1 else raw
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip()

            if raw and not raw.startswith("["):
                start = raw.find("[")
                end   = raw.rfind("]")
                if start != -1 and end > start:
                    log.warning("AI returned prose before JSON — extracting array from position %d", start)
                    raw = raw[start:end + 1].strip()

            decisions = json.loads(raw)
            if not isinstance(decisions, list):
                decisions = [decisions]

            for d in decisions:
                if "ticker" in d and "symbol" not in d:
                    d["symbol"] = d.pop("ticker")

            log.info("AI decisions received: %d", len(decisions))
            for d in decisions:
                log.info("  [%s] %s | conf=%s R:R=%s risk=$%s | %s",
                         d.get("final_decision", "?"),
                         d.get("symbol", "?"),
                         d.get("signal_confidence", "?"),
                         d.get("reward_to_risk", "?"),
                         d.get("risk_per_trade_dollars", "?"),
                         str(d.get("reason_for_entry", ""))[:80])
            return decisions

        except json.JSONDecodeError as e:
            log.error("AI returned invalid JSON: %s | raw=%s", e, raw[:400])
            return None
        except Exception as e:
            log.error("AI call failed (%s): %s", type(e).__name__, e)
            return None

    @staticmethod
    def rule_based_fallback(
        watchlist_data: list[dict],
        open_positions: list[dict],
    ) -> list[dict]:
        """Emit HOLD rows for open positions and BUY rows for top scores when LLM output is empty.

        Args:
            watchlist_data: Scored candidates after the pre-filter (symbol and signal_score required).
            open_positions: Snapshot rows so every open symbol receives a HOLD decision.

        Returns:
            A list of minimal decision dicts safe for execute_decisions in an outage.
        """
        decisions = []

        for pos in open_positions:
            sym = (pos.get("symbol") or "").upper()
            if sym:
                decisions.append({
                    "symbol":            sym,
                    "action":            "HOLD",
                    "final_decision":    "HOLD",
                    "signal_confidence": 7,
                    "reason_for_entry":  "Rule-based fallback: Claude unavailable — holding open position, mechanical stops active",
                })

        buys = 0
        max_buys = config.MAX_CONCURRENT_POSITIONS - len(open_positions)
        for item in watchlist_data:
            if buys >= max_buys:
                break
            sym   = (item.get("symbol") or "").upper()
            score = float(item.get("signal_score") or 0.0)
            if score >= 7.5 and sym:
                decisions.append({
                    "symbol":            sym,
                    "action":            "BUY",
                    "final_decision":    "BUY",
                    "signal_confidence": min(10, int(score)),
                    "setup_type":        item.get("setup_type_hint", "momentum"),
                    "reason_for_entry":  f"Rule-based fallback: score={score:.1f} ≥7.5, Claude unavailable",
                    "reason_to_avoid":   "",
                })
                buys += 1

        log.warning("Rule-based fallback fired: %d decisions (%d BUY, %d HOLD)",
                    len(decisions), buys, len(open_positions))
        return decisions
