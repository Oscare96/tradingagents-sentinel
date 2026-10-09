from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


def timestamp() -> str:
    return utcnow().isoformat()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Quote(StrictModel):
    symbol: str
    asset: Literal["stock", "crypto", "option"]
    underlying: str
    bid: float = Field(gt=0)
    ask: float = Field(gt=0)
    bid_size: float = Field(ge=0)
    ask_size: float = Field(ge=0)
    observed_at: datetime
    source_id: str
    previous_close: float | None = None
    daily_volume: float | None = None
    expiration: str | None = None
    strike: float | None = None
    option_type: str | None = None
    delta: float | None = None
    implied_volatility: float | None = None

    @model_validator(mode="after")
    def ordered(self):
        if self.bid > self.ask:
            raise ValueError("crossed quote")
        if self.observed_at.tzinfo is None:
            raise ValueError("quote requires timezone")
        return self


class Decision(StrictModel):
    symbol: str
    action: Literal["trade", "watch", "pass"]
    direction: Literal["long", "short"]
    style: Literal["day", "swing", "position"]
    entry_low: float = Field(gt=0)
    entry_high: float = Field(gt=0)
    stop: float = Field(gt=0)
    target: float = Field(gt=0)
    valid_minutes: int = Field(ge=1, le=1440)
    holding_hours: int = Field(ge=1, le=2160)
    evidence_ids: list[str] = Field(min_length=1)
    thesis: str = Field(min_length=10, max_length=2000)
    invalidation: str = Field(min_length=5, max_length=1000)
    counterargument: str = Field(min_length=5, max_length=1000)
    alternative: str = Field(min_length=5, max_length=1000)

    @model_validator(mode="after")
    def levels(self):
        if self.entry_low > self.entry_high:
            raise ValueError("entry range reversed")
        if (
            self.direction == "long"
            and not self.stop < self.entry_low <= self.entry_high < self.target
        ):
            raise ValueError("long levels must satisfy stop < entry range < target")
        if (
            self.direction == "short"
            and not self.target < self.entry_low <= self.entry_high < self.stop
        ):
            raise ValueError("short levels must satisfy target < entry range < stop")
        return self


class Briefing(StrictModel):
    summary: str = Field(max_length=4000)
    market_condition: str = Field(max_length=500)
    next_session: str = Field(max_length=2000)
    decisions: list[Decision] = Field(max_length=40)


class Controls(StrictModel):
    mode: Literal["watchlist", "auto"] = "watchlist"
    state: Literal["research", "enabled", "manage_only"] = "research"
    paper_capital: float = Field(default=10000, gt=0, le=10000000)
    risk_per_trade: float = Field(default=25, gt=0, le=100000)
    daily_loss_limit: float = Field(default=100, gt=0, le=1000000)
    experiment_loss_limit: float = Field(default=250, gt=0, le=1000000)
    max_position_pct: float = Field(default=10, gt=0, le=100)
    max_exposure_pct: float = Field(default=30, gt=0, le=100)
    max_positions: int = Field(default=3, ge=1, le=50)
    max_spread_bps: float = Field(default=100, gt=0, le=10000)
    max_quote_age_seconds: int = Field(default=60, ge=1, le=300)
    cost_bps_per_side: float = Field(default=15, ge=0, le=1000)
    allow_stock_short: bool = False
    enabled_assets: list[Literal["stock", "crypto", "option"]] = ["stock", "crypto", "option"]
