"""Regression tests using Home Assistant's actual flow, service and coordinator APIs."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.core import valid_entity_id
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components import pollen_lu
from custom_components.pollen_lu.config_flow import PollenLuConfigFlow
from custom_components.pollen_lu.const import DOMAIN
from custom_components.pollen_lu.sensor import PollenSensor


@pytest.mark.asyncio
async def test_options_flow(hass, entry):
    """Options use the read-only entry property supplied by HA."""
    hass.config_entries = Mock()
    hass.config_entries.async_get_known_entry.return_value = entry
    flow = PollenLuConfigFlow.async_get_options_flow(entry)
    flow.hass = hass
    flow.handler = entry.entry_id
    form = await flow.async_step_init()
    assert form["data_schema"]({}) == {"scan_interval": 30}
    result = await flow.async_step_init({"scan_interval": 45})
    assert result["data"] == {"scan_interval": 45}


@pytest.mark.asyncio
async def test_setup_awaits_platforms(hass, entry, monkeypatch):
    """Setup must remain pending until platform setup completes."""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def forward(*args):
        entered.set()
        await release.wait()

    coordinator = Mock(async_config_entry_first_refresh=AsyncMock())
    monkeypatch.setattr(pollen_lu, "MyCoordinator", Mock(return_value=coordinator))
    monkeypatch.setattr(pollen_lu, "async_get_clientsession", Mock())
    hass.config_entries = Mock(async_forward_entry_setups=AsyncMock(side_effect=forward))
    task = asyncio.create_task(pollen_lu.async_setup_entry(hass, entry))
    await entered.wait()
    try:
        assert not task.done()
    finally:
        release.set()
        await task
    assert task.result() is True
    assert len(entry.update_listeners) == 1


@pytest.mark.asyncio
async def test_setup_failure_cleans_coordinator(hass, entry, monkeypatch):
    monkeypatch.setattr(
        pollen_lu, "MyCoordinator",
        Mock(return_value=Mock(async_config_entry_first_refresh=AsyncMock())),
    )
    monkeypatch.setattr(pollen_lu, "async_get_clientsession", Mock())
    hass.config_entries = Mock(
        async_forward_entry_setups=AsyncMock(side_effect=RuntimeError("platform failed"))
    )
    with pytest.raises(RuntimeError, match="platform failed"):
        await pollen_lu.async_setup_entry(hass, entry)
    assert entry.entry_id not in hass.data[DOMAIN]


@pytest.mark.asyncio
@pytest.mark.parametrize("unload_ok", [True, False])
async def test_unload_preserves_shared_session(hass, entry, unload_ok):
    session = Mock(close=AsyncMock())
    coordinator = Mock(session=session)
    hass.data[DOMAIN] = {entry.entry_id: coordinator}
    hass.config_entries = Mock(async_unload_platforms=AsyncMock(return_value=unload_ok))
    assert await pollen_lu.async_unload_entry(hass, entry) is unload_ok
    assert (entry.entry_id in hass.data[DOMAIN]) is not unload_ok
    session.close.assert_not_called()


@pytest.mark.asyncio
async def test_reload_uses_ha_lifecycle(hass, entry):
    hass.config_entries = Mock(async_reload=AsyncMock(return_value=True))
    await pollen_lu.async_reload_entry(hass, entry)
    hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)


@pytest.mark.asyncio
async def test_service_resolves_reloaded_coordinator_and_optional_response(hass, entry):
    await pollen_lu.async_setup(hass, {})
    old = Mock(entry=entry, async_force_poll=AsyncMock())
    hass.data[DOMAIN] = {entry.entry_id: old}
    await hass.services.async_call(DOMAIN, "force_poll", {}, blocking=True)
    old.async_force_poll.assert_awaited_once()

    new = Mock(entry=entry, async_force_poll=AsyncMock())
    hass.data[DOMAIN][entry.entry_id] = new
    response = await hass.services.async_call(
        DOMAIN, "force_poll", {}, blocking=True, return_response=True
    )
    assert response == {"success": True}
    new.async_force_poll.assert_awaited_once()
    old.async_force_poll.assert_awaited_once()

    hass.data[DOMAIN].clear()
    with pytest.raises(ServiceValidationError, match="No Pollen.lu instances"):
        await hass.services.async_call(DOMAIN, "force_poll", {}, blocking=True)


@pytest.mark.asyncio
async def test_force_poll_notifies_listeners_and_reports_failure(hass, entry):
    coordinator = pollen_lu.MyCoordinator(hass, entry, Mock())
    assert coordinator.config_entry is entry
    listener = Mock()
    unsubscribe = coordinator.async_add_listener(listener)
    coordinator._async_update_data = AsyncMock(return_value={"success": True})
    try:
        assert await coordinator.async_force_poll() == {"success": True}
        listener.assert_called_once()
        assert coordinator.data == {"success": True}

        coordinator._async_update_data.side_effect = UpdateFailed("offline")
        with pytest.raises(HomeAssistantError, match="Failed to refresh"):
            await coordinator.async_force_poll()
        assert not coordinator.last_update_success
        assert listener.call_count == 2
    finally:
        unsubscribe()
        await coordinator.async_shutdown()


@pytest.mark.parametrize("key", ["Artemisia", "Betula", "Pollen With Spaces", "Chêne"])
def test_sensor_entity_ids_are_valid(key):
    sensor = PollenSensor(Mock(), {
        "id": 42, "translationKey": key, "pictures": [{"path": "test.svg"}],
    })
    assert valid_entity_id(sensor.entity_id)
    assert sensor.entity_id == sensor.entity_id.lower()
    assert sensor.unique_id == f"42_{key}"
    if key == "Artemisia":
        assert sensor.entity_id == "sensor.pollen_artemisia"
