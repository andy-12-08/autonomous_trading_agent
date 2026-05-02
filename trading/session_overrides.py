"""
Session-level threshold overrides — set once by the morning study, active all day.

The morning study analyses yesterday's missed opportunities and may propose small,
evidence-based adjustments to screening thresholds. This module applies those
values (clamped to safety bounds) and exposes them to the signal scorer and
risk manager via get().

Hard constraints (R:R < 2, RSI > 72, drawdown limit, circuit breaker) are
NEVER adjustable — they are enforced in code regardless of what is set here.
"""
from core.database import log


class SessionOverrides:
    """Manages session-level threshold overrides applied once per trading day.

    The morning study may propose small, evidence-based adjustments to screening
    thresholds. This class applies those values (clamped to safety bounds) and
    exposes them to the signal scorer and risk manager via get().

    Attributes:
        config: The config module containing default threshold values.
    """

    def __init__(self, config_module):
        """Initialize SessionOverrides with a config module.

        Args:
            config_module: The config module providing NORMAL_MIN_SIGNAL_SCORE,
                MIDDAY_MIN_SIGNAL_SCORE, and MIN_VOL_RATIO_ENTRY constants.
        """
        self.config = config_module

        # Safety bounds: (min_allowed, max_allowed)
        self._BOUNDS: dict[str, tuple[float, float]] = {
            "signal_score_min_normal": (5.5,  7.5),
            "signal_score_min_midday": (6.5,  8.5),
            "vol_ratio_min_entry":     (0.8,  1.5),
            "rsi_max_entry":           (62.0, 70.0),
        }

        self._DEFAULTS: dict[str, float] = {
            "signal_score_min_normal": config_module.NORMAL_MIN_SIGNAL_SCORE,
            "signal_score_min_midday": config_module.MIDDAY_MIN_SIGNAL_SCORE,
            "vol_ratio_min_entry":     float(config_module.MIN_VOL_RATIO_ENTRY),
            "rsi_max_entry":           65.0,
        }

        self._active: dict[str, float] = dict(self._DEFAULTS)

    def apply(self, plan: dict) -> dict[str, float]:
        """Read threshold_overrides from the daily plan and apply bounded values.

        Called once after morning study completes. Resets to defaults first,
        then applies any plan-specified overrides clamped to safety bounds.

        Args:
            plan: The daily plan dict, which may contain a 'threshold_overrides' key.

        Returns:
            The active overrides dict after applying plan values.
        """
        self._active = dict(self._DEFAULTS)
        raw = plan.get("threshold_overrides") or {}
        if not raw:
            return self._active

        for key, (lo, hi) in self._BOUNDS.items():
            if key in raw:
                try:
                    val     = float(raw[key])
                    clamped = max(lo, min(hi, val))
                    self._active[key] = clamped
                    if abs(clamped - self._DEFAULTS[key]) > 0.01:
                        log.info(
                            "SESSION OVERRIDE: %s = %.2f  (default %.2f, plan requested %.2f%s)",
                            key, clamped, self._DEFAULTS[key], val,
                            f" — clamped to bound [{lo},{hi}]" if clamped != val else "",
                        )
                except (TypeError, ValueError):
                    pass

        return self._active

    def get(self, key: str) -> float:
        """Return the active value for a threshold.

        Falls back to the default if the key is not set.

        Args:
            key: The threshold key to look up.

        Returns:
            The active float value for the given threshold key.
        """
        return self._active.get(key, self._DEFAULTS.get(key, 0.0))

    def reset(self):
        """Reset all overrides to config defaults.

        Called at the start of each trading day.
        """
        self._active = dict(self._DEFAULTS)

    def summary(self) -> str:
        """Return a human-readable summary of active overrides.

        Only changed values (differing from defaults by more than 0.01) are shown.

        Returns:
            A pipe-delimited string of changed overrides, or a message indicating
            no overrides are active.
        """
        changed = {k: v for k, v in self._active.items() if abs(v - self._DEFAULTS.get(k, 0)) > 0.01}
        if not changed:
            return "no overrides active (all defaults)"
        return " | ".join(f"{k}={v:.2f} (was {self._DEFAULTS[k]:.2f})" for k, v in changed.items())
