"""Deterministic fixture market-data provider.

Phase 0 spec s11 P0-F: implement :class:`MarketDataProvider` for fixtures first,
and keep provider-specific field names out of the domain.

The fixture provider generates every bar *deterministically* from
``(symbol, trade_date)`` using SHA-256, so:

* the same checkout always produces identical prices -- no network, no clock;
* fixtures work for any trading calendar the rules registry supplies;
* there is no hidden state to reset between tests.

The generated series is a slow sine wave plus small deterministic noise, which
keeps day-over-day moves well inside a +/-10% price-limit band so that risk and
fill logic can be exercised realistically. This is *simulation data*, not real
market history, and must never be described as such.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from janus.application.ports.market_data import ProviderHealth
from janus.domain.common.enums import AdjustmentMode, DataQualityStatus, InstrumentType, QuoteSource
from janus.domain.common.errors import ConfigurationError, NotFoundError
from janus.domain.common.time import MARKET_TZ, PointInTime
from janus.domain.instruments.models import Instrument
from janus.domain.instruments.symbol import validate_symbol
from janus.domain.market_data.models import MarketBar

__all__ = ["FixtureProvider", "load_instruments", "load_strategies"]

PROVIDER_ID = "fixture"

_BAR_CLOSE_TIME = time(15, 0)
_BAR_PUBLISHED_TIME = time(15, 5)
_BAR_RETRIEVED_TIME = time(15, 10)

# Deterministic wave parameters shared by every generated series.
_WAVE_PERIOD_DAYS = 40.0
_WAVE_AMPLITUDE = Decimal("0.12")
_NOISE_AMPLITUDE = Decimal("0.02")


def _digest(*parts: str) -> bytes:
    return hashlib.sha256("|".join(parts).encode("utf-8")).digest()


def _unit(*parts: str, offset: int = 0) -> Decimal:
    """A stable pseudo-random value in [0, 1) derived from the parts."""
    raw = _digest(*parts)
    chunk = raw[offset : offset + 8]
    if len(chunk) < 8:  # pragma: no cover - defensive
        chunk = raw[:8]
    return (Decimal(int.from_bytes(chunk, "big")) / Decimal(2**64)).quantize(Decimal("0.0000001"))


def _tick_for(instrument: Instrument) -> Decimal:
    if instrument.instrument_type is InstrumentType.DOMESTIC_EQUITY_ETF:
        return Decimal("0.001")
    return Decimal("0.01")


def _base_price(instrument: Instrument) -> Decimal:
    unit = _unit(instrument.symbol, "base")
    return (Decimal("8") + unit * Decimal("192")).quantize(Decimal("0.001"))


class FixtureProvider:
    """Deterministic, offline :class:`MarketDataProvider` implementation."""

    __slots__ = ("_fail_lookups", "_instruments")

    def __init__(self, instruments: Sequence[Instrument], *, fail_lookups: bool = False) -> None:
        self._instruments: dict[str, Instrument] = {i.symbol: i for i in instruments}
        if len(self._instruments) != len(instruments):
            raise ConfigurationError("duplicate symbol in fixture instrument master")
        self._fail_lookups = fail_lookups

    # -- MarketDataProvider ---------------------------------------------
    @property
    def provider_id(self) -> str:
        return PROVIDER_ID

    def get_daily_bar(self, symbol: str, trade_date: date) -> MarketBar | None:
        canonical = validate_symbol(symbol)
        instrument = self._instruments.get(canonical)
        if instrument is None:
            if self._fail_lookups:  # pragma: no cover - defensive
                raise NotFoundError("unknown fixture symbol", symbol=canonical)
            return None
        return self._generate(instrument, trade_date)

    def get_daily_bars(self, symbol: str, start: date, end: date) -> tuple[MarketBar, ...]:
        if end < start:
            return ()
        bars: list[MarketBar] = []
        cursor = start
        while cursor <= end:
            bar = self.get_daily_bar(symbol, cursor)
            if bar is not None:
                bars.append(bar)
            cursor += timedelta(days=1)
        return tuple(bars)

    def list_symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self._instruments))

    def instruments(self) -> tuple[Instrument, ...]:
        return tuple(self._instruments[s] for s in sorted(self._instruments))

    def instrument(self, symbol: str) -> Instrument | None:
        return self._instruments.get(validate_symbol(symbol))

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider_id=PROVIDER_ID,
            ok=bool(self._instruments),
            detail=f"{len(self._instruments)} fixture instruments",
            checked_at=datetime.now(tz=MARKET_TZ),
        )

    def close(self) -> None:
        return None

    # -- generation ------------------------------------------------------
    def _generate(self, instrument: Instrument, trade_date: date) -> MarketBar:
        tick = _tick_for(instrument)
        base = _base_price(instrument)
        symbol = instrument.symbol
        day_index = Decimal(trade_date.toordinal())

        phase = _unit(symbol, "phase") * Decimal(_WAVE_PERIOD_DAYS)
        angle = (
            (day_index + phase) / Decimal(_WAVE_PERIOD_DAYS) * Decimal("2") * Decimal(str(math.pi))
        )
        wave = Decimal(str(math.sin(float(angle))))

        def price(*parts: str, offset: int = 0) -> Decimal:
            noise = (
                (_unit(symbol, trade_date.isoformat(), *parts, offset=offset) - Decimal("0.5"))
                * Decimal("2")
                * _NOISE_AMPLITUDE
            )
            value = base * (Decimal("1") + _WAVE_AMPLITUDE * wave + noise)
            quantized = value.quantize(tick)
            return quantized if quantized > 0 else tick

        close = price("close")
        open_ = price("open", offset=8)
        high = max(open_, close) * (
            Decimal("1")
            + Decimal("0.002")
            + _unit(symbol, trade_date.isoformat(), "high") * Decimal("0.010")
        )
        low = min(open_, close) * (
            Decimal("1")
            - Decimal("0.002")
            - _unit(symbol, trade_date.isoformat(), "low") * Decimal("0.010")
        )
        high = max(high.quantize(tick), open_, close)
        low = min(low.quantize(tick), open_, close)
        if low <= 0:  # pragma: no cover - defensive
            low = tick

        volume_units = Decimal("400000") + _unit(symbol, trade_date.isoformat(), "vol") * Decimal(
            "1600000"
        )
        volume = int(volume_units)
        vwap = (open_ + close + high + low) / Decimal("4")
        amount = (vwap * Decimal(volume)).quantize(Decimal("0.01"))

        close_dt = datetime.combine(trade_date, _BAR_CLOSE_TIME, tzinfo=MARKET_TZ)
        published = datetime.combine(trade_date, _BAR_PUBLISHED_TIME, tzinfo=MARKET_TZ)
        retrieved = datetime.combine(trade_date, _BAR_RETRIEVED_TIME, tzinfo=MARKET_TZ)

        return MarketBar(
            symbol=symbol,
            trade_date=trade_date,
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=volume,
            amount=amount,
            adjustment=AdjustmentMode.UNADJUSTED,
            quality=DataQualityStatus.OK,
            source=QuoteSource.FIXTURE,
            source_ref=f"fixture:{symbol}:{trade_date.isoformat()}",
            raw_payload_hash=hashlib.sha256(
                f"{symbol}:{trade_date.isoformat()}:fixture".encode()
            ).hexdigest(),
            point_in_time=PointInTime(
                event_time=close_dt,
                published_at=published,
                retrieved_at=retrieved,
                known_at=retrieved,
            ),
        )


def load_instruments(path: Path) -> tuple[Instrument, ...]:
    """Load the fixture instrument master from JSON."""
    import json

    if not path.is_file():
        raise ConfigurationError("fixture instruments file is missing", file=str(path))
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ConfigurationError("fixture instruments must be a JSON array", file=str(path))
    try:
        instruments = tuple(Instrument.model_validate(entry) for entry in payload)
    except Exception as exc:
        raise ConfigurationError(
            "invalid fixture instrument entry", file=str(path), error=str(exc)
        ) from exc
    symbols = [i.symbol for i in instruments]
    if len(symbols) != len(set(symbols)):
        raise ConfigurationError("duplicate fixture instrument symbol", file=str(path))
    return instruments


def load_strategies(path: Path) -> tuple[Mapping[str, object], ...]:
    """Load raw strategy definitions (validated by the caller's registry)."""
    import json

    if not path.is_file():
        raise ConfigurationError("fixture strategies file is missing", file=str(path))
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ConfigurationError("fixture strategies must be a JSON array", file=str(path))
    return tuple(payload)
