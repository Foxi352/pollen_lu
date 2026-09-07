"""Shared fixtures for tests against Home Assistant's actual APIs."""

from types import MappingProxyType

import pytest
import pytest_asyncio
from homeassistant import loader
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import frame

from custom_components.pollen_lu.const import DOMAIN


@pytest_asyncio.fixture
async def hass(tmp_path):
    """Initialize HA services and helpers without starting external integrations."""
    instance = HomeAssistant(str(tmp_path))
    frame.async_setup(instance)
    loader.async_setup(instance)
    yield instance
    await instance.async_stop()


@pytest.fixture
def entry():
    return ConfigEntry(
        domain=DOMAIN,
        title="Pollen.lu",
        data={"name": "Pollen.lu"},
        options={"scan_interval": 30},
        source="user",
        unique_id="Pollen.lu",
        version=2,
        minor_version=1,
        discovery_keys=MappingProxyType({}),
        subentries_data=None,
        state=ConfigEntryState.LOADED,
    )
