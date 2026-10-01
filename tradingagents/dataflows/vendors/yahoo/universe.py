"""Batched multi-ticker Yahoo downloads for universe screens.

The scanner needs one daily frame and one intraday frame covering hundreds of
tickers every cycle; per-symbol data-layer calls could never keep up, so the
Yahoo vendor package offers this one batched entry point. Like every vendor
call, a failure raises a VendorError subclass: an outage must read as an
outage, never as a fact about the market.
"""

from __future__ import annotations

import logging

import pandas as pd
import yfinance as yf

from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.vendors.yahoo.common import yf_retry

logger = logging.getLogger(__name__)


def download_batch(
    tickers: list[str],
    *,
    period: str,
    interval: str,
    auto_adjust: bool = True,
) -> pd.DataFrame:
    """Download one multi-ticker OHLCV frame via yfinance.

    Raises:
        VendorUnavailableError: the request failed or came back empty.
    """
    try:
        frame = yf_retry(
            lambda: yf.download(
                tickers,
                period=period,
                interval=interval,
                auto_adjust=auto_adjust,
                progress=False,
                threads=True,
            )
        )
    except Exception as exc:
        raise VendorUnavailableError(
            f"Yahoo batch download failed ({period}/{interval}): {exc}"
        ) from exc
    if frame is None or frame.empty:
        raise VendorUnavailableError(
            f"Yahoo batch download returned no data ({period}/{interval})"
        )
    return frame
