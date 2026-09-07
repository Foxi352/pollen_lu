"""Exercise incomplete API responses and configuration validation."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.config_entries import ConfigEntriesFlowManager
from homeassistant.loader import Integration
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components import pollen_lu
from custom_components.pollen_lu import sensor as sensor_platform
from custom_components.pollen_lu.config_flow import PollenLuConfigFlow
from custom_components.pollen_lu.const import DOMAIN, DEFAULT_SCAN_INTERVAL
from custom_components.pollen_lu.sensor import PollenSensor

@pytest.fixture
def record():
    return {"id": 42, "translationKey": "Artemisia", "active": True, "value": 12.7}


@pytest.fixture
def sensor(record):
    coordinator = Mock(
        pollen=[record], translations=[], last_poll=None, next_poll=None,
        last_update_success=True,
    )
    return PollenSensor(coordinator, record)


@pytest.mark.parametrize("value, expected", [
    (12.7, 13), (0, 0), ("14.2", 14), (None, None), ("", None),
    ("bad", None), (float("nan"), None), (float("inf"), None),
    (-1, None), (True, None), ({}, None), ([], None),
])
def test_native_value(sensor, record, value, expected):
    record["value"] = value
    assert sensor.native_value == expected
    assert sensor.state == expected  # SensorEntity consumes native_value.


def test_undetected_is_zero_and_missing_value_is_unknown(sensor, record):
    record.pop("value")
    assert sensor.native_value is None
    record["level"] = "undetected"
    assert sensor.native_value == 0


@pytest.mark.asyncio
async def test_inactive_missing_and_recovered_sensor(hass, sensor, record):
    sensor.hass = hass
    assert sensor.available
    record["active"] = False
    assert not sensor.available
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {"last_poll": None, "next_poll": None}
    sensor.coordinator.pollen = []
    assert not sensor.available
    assert sensor.native_value is None
    assert sensor.entity_picture is None
    assert sensor.extra_state_attributes == {"last_poll": None, "next_poll": None}
    record["active"] = True
    sensor.coordinator.pollen = [record]
    assert sensor.available
    assert sensor.native_value == 13
    sensor.coordinator.last_update_success = False
    assert not sensor.available


@pytest.mark.asyncio
@pytest.mark.parametrize("optional", [None, [], {}, "bad", [None, {}, 1]])
async def test_missing_optional_data(hass, sensor, record, optional):
    sensor.hass = hass
    record.update(pictures=optional, descriptions=optional, threshold=optional)
    sensor.coordinator.translations = optional
    assert sensor.name == "Pollen Artemisia"
    assert sensor.entity_picture is None
    attributes = sensor.extra_state_attributes
    assert "description" not in attributes
    assert "moderate_threshold" not in attributes
    assert "high_threshold" not in attributes


@pytest.mark.asyncio
async def test_translations_and_partial_thresholds(hass, sensor, record):
    sensor.hass = hass
    hass.config.language = "de"
    record.update(
        pictures=[{}, {"path": "test.svg"}], descriptions=[None, "description_key"],
        threshold=[{}, {"type": "medium", "min": 11}, {"type": "high"}],
    )
    sensor.coordinator.translations = [None, {
        "key": "Artemisia", "domain": "pollen",
        "translations": [None, {}, {"locale": "de", "content": "Beifuß"}],
    }]
    assert sensor.name == "Pollen Beifuß"
    assert sensor.entity_picture == "test.svg"
    assert sensor.extra_state_attributes["moderate_threshold"] == 11
    assert "high_threshold" not in sensor.extra_state_attributes
    assert sensor.extra_state_attributes["description"] == "description_key"
    hass.config.language = "fr"
    assert sensor.name == "Pollen Artemisia"


@pytest.mark.asyncio
async def test_platform_skips_unidentifiable_records(hass, entry, record):
    coordinator = Mock(pollen=[None, {}, {"active": True}, record])
    hass.data[DOMAIN] = {entry.entry_id: coordinator}
    add_entities = Mock()
    await sensor_platform.async_setup_entry(hass, entry, add_entities)
    entities = add_entities.call_args.args[0]
    assert len(entities) == 1
    assert entities[0].unique_id == "42_Artemisia"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, -1, 1.5, "30", True, None])
async def test_reject_invalid_options(hass, entry, value):
    hass.config_entries = Mock()
    hass.config_entries.async_get_known_entry.return_value = entry
    flow = PollenLuConfigFlow.async_get_options_flow(entry)
    flow.hass = hass
    flow.handler = entry.entry_id
    result = await flow.async_step_init({"scan_interval": value})
    assert result["type"] == "form"
    assert result["errors"] == {"scan_interval": "invalid_scan_interval"}


@pytest.mark.asyncio
async def test_old_invalid_interval_falls_back_without_mutating_entry(hass, entry):
    # Represents a value persisted by an older integration version.
    old_entry = Mock(options={"scan_interval": 0}, data={}, async_on_unload=Mock())
    coordinator = pollen_lu.MyCoordinator(hass, old_entry, Mock())
    assert coordinator.update_interval.total_seconds() == DEFAULT_SCAN_INTERVAL * 60
    assert old_entry.options["scan_interval"] == 0
    await coordinator.async_shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, {}, {"data": None}, {"data": {}}, {"data": "bad"}])
async def test_bad_payload_preserves_last_good_data(hass, entry, record, payload):
    response = Mock(json=AsyncMock(return_value=payload))
    context = AsyncMock()
    context.__aenter__.return_value = response
    session = Mock(get=Mock(return_value=context))
    coordinator = pollen_lu.MyCoordinator(hass, entry, session)
    coordinator.pollen = [record]
    coordinator.translations = []
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert coordinator.pollen == [record]
    with pytest.raises(UpdateFailed):
        await coordinator._async_setup()
    assert coordinator.translations == []
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_manifest_prevents_second_instance(hass, monkeypatch):
    path = Path(pollen_lu.__file__).parent
    manifest = json.loads((path / "manifest.json").read_text())
    integration = Integration(hass, "custom_components.pollen_lu", path, manifest)
    monkeypatch.setattr(
        "homeassistant.loader.async_get_integration", AsyncMock(return_value=integration)
    )
    entries = Mock(async_has_entries=Mock(return_value=True))
    manager = ConfigEntriesFlowManager(hass, entries, {})
    result = await manager.async_init(
        DOMAIN, context={"source": "user"}, data={"name": "Another name"}
    )
    assert result["type"] == "abort"
    assert result["reason"] == "single_instance_allowed"
