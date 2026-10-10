"""Regression tests for network failures, coordinator logging and recovery."""

from datetime import datetime, timezone
import logging
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components import pollen_lu
from custom_components.pollen_lu.const import API_URL, DOMAIN
from custom_components.pollen_lu.sensor import PollenSensor


def request_context(payload=None, *, error=None, json_error=None):
    """Simulate a request or response-body failure without using a live API."""
    response = Mock(json=AsyncMock(return_value=payload, side_effect=json_error))
    context = AsyncMock()
    context.__aenter__.return_value = response
    context.__aenter__.side_effect = error
    return context


def http_error(status, headers=None):
    return aiohttp.ClientResponseError(
        Mock(real_url=f"{API_URL}/pollens"), (),
        status=status, message="upstream error", headers=headers,
    )


@pytest.fixture
def record():
    return {
        "id": 42, "translationKey": "Artemisia", "active": True,
        "value": 17, "lastMeasurementDate": "2026-10-10 09:00:00",
    }


@pytest.mark.asyncio
async def test_timeout_is_scoped_to_both_requests(hass, entry, record):
    session = Mock(get=Mock(side_effect=[
        request_context({"data": []}), request_context({"data": [record]}),
    ]))
    original_session_timeout = session.timeout
    coordinator = pollen_lu.MyCoordinator(hass, entry, session)
    try:
        await coordinator._async_setup()
        await coordinator._async_update_data()
        assert coordinator.translations == []
        assert coordinator.pollen == [record]
        assert coordinator.last_poll is not None
        for endpoint, call in zip(("translations", "pollens"), session.get.call_args_list):
            assert call.args == (f"{API_URL}/{endpoint}",)
            assert call.kwargs["headers"] == coordinator.headers
            assert call.kwargs["timeout"].total == 30
            assert call.kwargs["timeout"].connect == 10
        assert session.timeout is original_session_timeout
    finally:
        await coordinator.async_shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["translations", "pollens"])
@pytest.mark.parametrize("kind, expected", [
    ("timeout", "Timed out"),
    ("body_timeout", "Timed out"),
    ("connection", "Connection error"),
    ("429", "rate limited (HTTP 429)"),
    ("504", "server error (HTTP 504)"),
    ("403", "request rejected (HTTP 403)"),
    ("invalid_json", "Invalid"),
    ("content_type", "Invalid"),
    ("bad_payload", "expected a data list"),
])
async def test_expected_failures_are_concise_and_do_not_log_twice(
    hass, entry, record, caplog, endpoint, kind, expected
):
    if kind.isdigit():
        context = request_context(error=http_error(int(kind)))
    elif kind == "timeout":
        context = request_context(error=TimeoutError())
    elif kind == "body_timeout":
        context = request_context(json_error=TimeoutError())
    elif kind == "connection":
        context = request_context(error=aiohttp.ClientConnectionError("offline"))
    elif kind == "invalid_json":
        context = request_context(json_error=ValueError("invalid JSON"))
    elif kind == "content_type":
        context = request_context(json_error=aiohttp.ContentTypeError(
            Mock(real_url=f"{API_URL}/{endpoint}"), (),
            status=200, message="unexpected content type",
        ))
    else:
        context = request_context({"data": None})
    session = Mock(get=Mock(return_value=context))
    coordinator = pollen_lu.MyCoordinator(hass, entry, session)
    coordinator.pollen = [record]
    coordinator.translations = [{"key": "cached"}]
    coordinator.last_poll = "last successful fetch"
    caplog.set_level(logging.ERROR, logger=pollen_lu.__name__)
    try:
        fetch = coordinator._async_setup if endpoint == "translations" else coordinator._async_update_data
        with pytest.raises(UpdateFailed) as raised:
            await fetch()
        assert endpoint in str(raised.value)
        assert expected in str(raised.value)
        assert raised.value.__cause__ is not None
        assert coordinator.pollen == [record]
        assert coordinator.translations == [{"key": "cached"}]
        assert coordinator.last_poll == "last successful fetch"
        assert not [item for item in caplog.records if item.name == pollen_lu.__name__]
    finally:
        await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_consecutive_failures_log_once_and_recover(
    hass, entry, record, caplog, monkeypatch
):
    recovered = {**record, "value": 23, "lastMeasurementDate": "2026-10-10 12:00:00"}
    session = Mock(get=Mock(side_effect=[
        request_context({"data": [record]}),
        request_context(error=http_error(504)),
        request_context(error=TimeoutError()),
        request_context(error=http_error(504)),
        request_context({"data": [recovered]}),
        request_context(error=http_error(504)),
    ]))
    clock = Mock()
    clock.now.return_value = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(pollen_lu, "datetime", clock)
    coordinator = pollen_lu.MyCoordinator(hass, entry, session)
    listener = Mock()
    unsubscribe = coordinator.async_add_listener(listener)
    caplog.set_level(logging.INFO, logger=pollen_lu.__name__)
    try:
        await coordinator.async_refresh()
        sensor = PollenSensor(coordinator, record)
        sensor.hass = hass
        last_poll = coordinator.last_poll
        for _ in range(3):
            await coordinator.async_refresh()
            assert not coordinator.last_update_success
            assert not sensor.available
            assert coordinator.pollen == [record]
            assert sensor.native_value == 17
            assert sensor.extra_state_attributes["last_update"] == record["lastMeasurementDate"]
            assert coordinator.last_poll == last_poll
        errors = [item for item in caplog.records if item.levelno >= logging.ERROR]
        assert len(errors) == 1
        assert "HTTP 504" in errors[0].getMessage()
        assert listener.call_count == 2  # Initial success and transition to unavailable.

        clock.now.return_value = datetime(2026, 10, 10, 13, tzinfo=timezone.utc)
        await coordinator.async_refresh()
        assert coordinator.last_update_success
        assert sensor.available
        assert sensor.native_value == 23
        assert coordinator.last_poll != last_poll
        assert sensor.extra_state_attributes["last_update"] == recovered["lastMeasurementDate"]
        assert "next_poll" not in sensor.extra_state_attributes
        assert any("recovered" in item.getMessage() for item in caplog.records)
        assert listener.call_count == 3
        assert coordinator.update_interval.total_seconds() == 30 * 60

        # A separate outage must produce a new visible initial failure.
        await coordinator.async_refresh()
        assert len([item for item in caplog.records if item.levelno >= logging.ERROR]) == 2
    finally:
        unsubscribe()
        await coordinator.async_shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["translations", "pollens"])
@pytest.mark.parametrize("retry_after", [None, "7200"])
async def test_first_refresh_failure_is_retryable_and_does_not_forward_platforms(
    hass, entry_factory, monkeypatch, caplog, endpoint, retry_after
):
    entry = entry_factory(state=ConfigEntryState.SETUP_IN_PROGRESS)
    contexts = [request_context(error=http_error(504, {"Retry-After": retry_after}))]
    if endpoint == "pollens":
        contexts.insert(0, request_context({"data": []}))
    session = Mock(get=Mock(side_effect=contexts))
    monkeypatch.setattr(pollen_lu, "async_get_clientsession", Mock(return_value=session))
    hass.config_entries = Mock(async_forward_entry_setups=AsyncMock())
    caplog.set_level(logging.ERROR, logger=pollen_lu.__name__)
    with pytest.raises(ConfigEntryNotReady) as raised:
        await pollen_lu.async_setup_entry(hass, entry)
    assert isinstance(raised.value.__cause__, UpdateFailed)
    assert endpoint in str(raised.value.__cause__)
    assert not hass.data.get(DOMAIN)
    hass.config_entries.async_forward_entry_setups.assert_not_called()
    session.close.assert_not_called()
    # HA's entry setup machinery reports retries; the integration adds no error.
    assert not [item for item in caplog.records if item.levelno >= logging.ERROR]
    for callback in entry._on_unload or []:
        result = callback()
        if result is not None:
            await result


@pytest.mark.asyncio
async def test_force_poll_makes_one_attempt_and_reports_failure(hass, entry, record):
    session = Mock(get=Mock(side_effect=[
        request_context({"data": [record]}), request_context(error=http_error(504)),
    ]))
    coordinator = pollen_lu.MyCoordinator(hass, entry, session)
    try:
        await coordinator.async_refresh()
        last_poll = coordinator.last_poll
        with pytest.raises(HomeAssistantError, match="Failed to refresh"):
            await coordinator.async_force_poll()
        assert session.get.call_count == 2
        assert not coordinator.last_update_success
        assert coordinator.pollen == [record]
        assert coordinator.last_poll == last_poll
    finally:
        await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_unexpected_errors_keep_their_traceback(hass, entry, caplog):
    session = Mock(get=Mock(side_effect=RuntimeError("unexpected bug")))
    coordinator = pollen_lu.MyCoordinator(hass, entry, session)
    caplog.set_level(logging.ERROR, logger=pollen_lu.__name__)
    try:
        await coordinator.async_refresh()
        assert isinstance(coordinator.last_exception, RuntimeError)
        assert not isinstance(coordinator.last_exception, UpdateFailed)
        errors = [item for item in caplog.records if item.levelno >= logging.ERROR]
        assert len(errors) == 1
        assert "Unexpected error" in errors[0].getMessage()
        assert errors[0].exc_info is not None
    finally:
        await coordinator.async_shutdown()
