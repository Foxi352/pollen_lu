"""Server retry hints use HA's coordinator scheduling without changing options."""

from datetime import datetime, timezone
from unittest.mock import Mock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed
from multidict import CIMultiDict

from custom_components import pollen_lu
from .test_outages import http_error, request_context


@pytest.fixture
def fixed_clock(monkeypatch):
    now = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(pollen_lu, "utcnow", lambda: now)


@pytest.mark.parametrize("header, expected", [
    ("7200", 7200),
    (" 7200 ", 7200),
    ("Sat, 10 Oct 2026 14:00:00 GMT", 7200),
    ("Saturday, 10-Oct-26 14:00:00 GMT", 7200),
    ("Sat Oct 10 14:00:00 2026", 7200),
    (None, None),
    ("", None),
    ("garbage", None),
    ("0", None),
    ("-1", None),
    ("1.5", None),
    ("NaN", None),
    ("inf", None),
    ("9" * 400, None),
    ("Sat, 10 Oct 2026 12:00:00 GMT", None),
    ("Sat, 10 Oct 2026 11:00:00 GMT", None),
])
def test_parse_retry_after(header, expected, fixed_clock):
    assert pollen_lu._parse_retry_after(header) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("status, header, expected", [
    (429, "7200", 7200),
    (503, "7200", 7200),
    (504, "Sat, 10 Oct 2026 14:00:00 GMT", 7200),
    (503, "300", 1800),
    (429, None, None),
    (503, "invalid", None),
    (503, "0", None),
    (503, "Sat, 10 Oct 2026 11:00:00 GMT", None),
    (403, "7200", None),
])
async def test_retry_hint_on_http_failure(hass, entry, fixed_clock, status, header, expected):
    # aiohttp supplies a case-insensitive headers mapping, including on errors.
    headers = CIMultiDict({"retry-after": header}) if header is not None else None
    context = request_context()
    context.__aenter__.return_value.raise_for_status.side_effect = http_error(status, headers)
    coordinator = pollen_lu.MyCoordinator(hass, entry, Mock(get=Mock(return_value=context)))
    try:
        with pytest.raises(UpdateFailed) as raised:
            await coordinator._async_update_data()
        assert raised.value.retry_after == expected
        assert coordinator.update_interval.total_seconds() == 1800
    finally:
        await coordinator.async_shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("manual_recovery", [False, True])
async def test_native_scheduler_waits_then_resumes_normal_interval(hass, entry, manual_recovery):
    session = Mock(get=Mock(side_effect=[
        request_context({"data": []}),
        request_context(error=http_error(503, {"Retry-After": "7200"})),
        request_context({"data": []}),
    ]))
    coordinator = pollen_lu.MyCoordinator(hass, entry, session)
    unsubscribe = coordinator.async_add_listener(Mock())
    try:
        await coordinator.async_refresh()
        with patch.object(hass.loop, "call_at", wraps=hass.loop.call_at) as schedule:
            await coordinator.async_refresh()
            assert not coordinator.last_update_success
            assert 7199 <= schedule.call_args.args[0] - hass.loop.time() <= 7201
            assert coordinator.update_interval.total_seconds() == 1800
            if manual_recovery:
                # A service request need not wait for the scheduled retry.
                await coordinator.async_force_poll()
            else:
                # Simulate expiry of the scheduled retry without waiting two hours.
                coordinator._unsub_refresh()
                await coordinator._handle_refresh_interval()
            assert coordinator.last_update_success
            assert 1799 <= schedule.call_args.args[0] - hass.loop.time() <= 1801
            assert session.get.call_count == 3
    finally:
        unsubscribe()
        await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_failed_manual_poll_reschedules_server_delay(hass, entry):
    session = Mock(get=Mock(side_effect=[
        request_context(error=http_error(429, {"Retry-After": "7200"})),
        request_context(error=http_error(429, {"Retry-After": "10800"})),
    ]))
    coordinator = pollen_lu.MyCoordinator(hass, entry, session)
    unsubscribe = coordinator.async_add_listener(Mock())
    try:
        await coordinator.async_refresh()
        with patch.object(hass.loop, "call_at", wraps=hass.loop.call_at) as schedule:
            with pytest.raises(HomeAssistantError, match="Failed to refresh"):
                await coordinator.async_force_poll()
            assert session.get.call_count == 2
            assert 10799 <= schedule.call_args.args[0] - hass.loop.time() <= 10801
            assert coordinator.update_interval.total_seconds() == 1800
    finally:
        unsubscribe()
        await coordinator.async_shutdown()
