from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from datetime import timedelta, datetime
import logging

from .const import DOMAIN, API_URL, DEFAULT_SCAN_INTERVAL, is_valid_scan_interval

_LOGGER = logging.getLogger(__name__)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

async def async_setup(hass, config: dict) -> bool:
    """Set up the integration."""
    _LOGGER.debug("async_setup()")

    async def handle_force_poll_service(call: ServiceCall) -> dict | None:
        """Refresh the currently loaded integration instances."""
        coordinators = [
            coordinator
            for coordinator in hass.data.get(DOMAIN, {}).values()
            if coordinator.entry.state is ConfigEntryState.LOADED
        ]
        if not coordinators:
            raise ServiceValidationError("No Pollen.lu instances are loaded")

        for coordinator in coordinators:
            await coordinator.async_force_poll()

        result = {"success": True}
        hass.states.async_set("pollen_lu.force_poll", result)
        if call.return_response:
            return result
        return None

    hass.services.async_register(
        DOMAIN,
        "force_poll",
        handle_force_poll_service,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True

async def async_setup_entry(hass, entry) -> bool:
    """Set up the integration from a config entry."""
    _LOGGER.debug("async_setup_entry()")
    session = async_get_clientsession(hass)
    coordinator = MyCoordinator(hass, entry, session)
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    try:
        await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])
    except BaseException:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        raise
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True

async def async_unload_entry(hass, entry):
    """Unload a config entry."""
    _LOGGER.debug("async_unload_entry()")
    if entry.entry_id in hass.data.get(DOMAIN, {}):
        unload_ok = await hass.config_entries.async_unload_platforms(entry, ["sensor"])
        if unload_ok:
            hass.data[DOMAIN].pop(entry.entry_id)
            if not hass.data[DOMAIN]:
                hass.states.async_remove("pollen_lu.force_poll")
            return True
        return False
    _LOGGER.warning(f"Attempted to unload entry {entry.entry_id} that was not loaded.")
    return False

async def async_reload_entry(hass, entry):
    """Reload config entry when options are updated."""
    _LOGGER.debug("async_reload_entry()")
    await hass.config_entries.async_reload(entry.entry_id)

class MyCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry, session):
        """Initialize the coordinator."""
        self.session = session
        self.entry = entry
        self.translations = None
        self.pollen = None
        self.last_poll = None
        self.next_poll = None
        self.headers = {
            "Host": "pollen-api.chl.lu",
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Sec-Fetch-Site": "cross-site",
            "Origin": "capacitor://localhost",
            "Accept-Encoding": "gzip, deflate, br",
            "Sec-Fetch-Mode": "cors",
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
            "Accept-Language": "lb,en-GB;q=0.9,en;q=0.8",
            "Sec-Fetch-Dest": "empty",
            "Connection": "keep-alive",
        }

        scan_interval = entry.options.get(
            CONF_SCAN_INTERVAL, entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )
        if not is_valid_scan_interval(scan_interval):
            _LOGGER.warning(
                "Invalid saved polling interval %r; using %s minutes until corrected in options",
                scan_interval, DEFAULT_SCAN_INTERVAL,
            )
            scan_interval = DEFAULT_SCAN_INTERVAL
        update_interval = timedelta(minutes=scan_interval)
        _LOGGER.info(f"Polling pollen.lu API every {scan_interval} minutes")

        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=update_interval,
        )
 
    async def _async_setup(self) -> None:
        """Fetch translations from API endpoint."""
        _LOGGER.debug("_async_setup()")
        try:
            async with self.session.get(f"{API_URL}/translations", headers=self.headers) as response:
                response.raise_for_status()
                payload = await response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                    raise ValueError("Expected a translations data list")
                self.translations = payload["data"]
                _LOGGER.debug("Translations fetched")
        except Exception as err:
            _LOGGER.error(f"Error fetching translations: {err}")
            raise UpdateFailed(f"Error fetching translations: {err}") from err

    async def _async_update_data(self):
        """Fetch data from API endpoint."""
        _LOGGER.debug("_async_update_data()")
        try:
            async with self.session.get(f"{API_URL}/pollens", headers=self.headers) as response:
                response.raise_for_status()
                payload = await response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                    raise ValueError("Expected a pollen data list")
                self.pollen = payload["data"]
                self.last_poll = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
                self.next_poll = (datetime.now().astimezone() + self.update_interval).strftime("%Y-%m-%d %H:%M:%S")
                _LOGGER.debug("Pollen fetched")
        except Exception as err:
            _LOGGER.error(f"Error fetching pollen counts: {err}")
            raise UpdateFailed(f"Error fetching pollen counts: {err}") from err
            
        return {"success": True}

    async def async_force_poll(self) -> dict:
        """Handle the action call to force poll the API."""
        _LOGGER.info("Force poll action called")
        await self.async_refresh()
        if not self.last_update_success:
            raise HomeAssistantError("Failed to refresh Pollen.lu data") from self.last_exception
        return {"success": True}
