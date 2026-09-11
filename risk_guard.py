"""Hard safety gates for Hyperliquid Final V1.
This module has no exchange credentials and can be unit-tested offline.
"""
from dataclasses import dataclass
from typing import Optional

@dataclass
class GateResult:
    allowed: bool
    reason: str = ""


def live_entry_gate(*, mode: str, agent_ok: bool, coin: str, whitelist: set,
                    leverage: float, max_leverage: float, margin: float,
                    capital: float, used_margin: float, max_total_risk: float,
                    position_count: int, max_positions: int,
                    backstop_enabled: bool, require_backstop: bool,
                    market_healthy: bool = True) -> GateResult:
    if mode != "live":
        return GateResult(False, "mode is not live")
    if not agent_ok:
        return GateResult(False, "Hyperliquid agent is not approved/healthy")
    if coin not in whitelist:
        return GateResult(False, f"{coin} is not in live whitelist")
    if leverage > max_leverage:
        return GateResult(False, f"leverage {leverage}x exceeds hard cap {max_leverage}x")
    if position_count >= max_positions:
        return GateResult(False, "maximum live positions reached")
    if capital <= 0 or margin <= 0:
        return GateResult(False, "invalid capital/margin")
    if used_margin + margin > capital * max_total_risk:
        return GateResult(False, "portfolio risk budget exceeded")
    if require_backstop and not backstop_enabled:
        return GateResult(False, "exchange backstop stop-loss is required")
    if not market_healthy:
        return GateResult(False, "market/exchange health gate is closed")
    return GateResult(True, "ok")


def require_verified_protection(*, sl_installed: bool, require_backstop: bool) -> GateResult:
    if require_backstop and not sl_installed:
        return GateResult(False, "position is not protected by a verified exchange stop")
    return GateResult(True, "ok")
