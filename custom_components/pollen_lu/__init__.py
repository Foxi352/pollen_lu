from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from datetime import timedelta, datetime, timezone
from email.utils import parsedate_to_datetime
import logging
import math
import aiohttp
from homeassistant.util.dt import utcnow

from .const import DOMAIN, API_URL, DEFAULT_SCAN_INTERVAL, is_valid_scan_interval

_LOGGER = logging.getLogger(__name__)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)


def _parse_retry_after(value: str | None) -> float | None:
    """Read a positive delay in seconds or an HTTP date; ignore invalid values."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        if value.isascii() and value.isdigit():
            delay = float(int(value))
        else:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                # HTTP dates, including the older asctime format, are in GMT.
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            delay = (retry_at - utcnow()).total_seconds()
    except (ValueError, TypeError, OverflowError):
        return None
    return delay if math.isfinite(delay) and delay > 0 else None

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
 
    async def _async_fetch_data(self, endpoint: str) -> list:
        """Fetch and validate an API list; the coordinator handles outage logging."""
        try:
            async with self.session.get(
                f"{API_URL}/{endpoint}",
                headers=self.headers,
                timeout=REQUEST_TIMEOUT,
            ) as response:
                response.raise_for_status()
                payload = await response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                    raise ValueError("expected a data list")
                return payload["data"]
        except TimeoutError as err:
            raise UpdateFailed(f"Timed out fetching {endpoint}") from err
        except (ValueError, aiohttp.ContentTypeError) as err:
            raise UpdateFailed(f"Invalid {endpoint} response: {err}") from err
        except aiohttp.ClientResponseError as err:
            retry_after = None
            if err.status == 429:
                reason = "rate limited"
            elif err.status >= 500:
                reason = "server error"
            else:
                reason = "request rejected"
            if err.status == 429 or err.status >= 500:
                header = err.headers.get("Retry-After") if err.headers else None
                delay = _parse_retry_after(header)
                if delay is not None:
                    # A short server delay must not increase the polling frequency.
                    retry_after = max(delay, self.update_interval.total_seconds())
            raise UpdateFailed(
                f"{endpoint} API {reason} (HTTP {err.status})",
                retry_after=retry_after,
            ) from err
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Connection error fetching {endpoint}") from err

    async def _async_setup(self) -> None:
        """Fetch translations from API endpoint."""
        self.translations = await self._async_fetch_data("translations")
        _LOGGER.debug("Translations fetched")

    async def _async_update_data(self):
        """Fetch data from API endpoint."""
        _LOGGER.debug("_async_update_data()")
        pollen = await self._async_fetch_data("pollens")
        last_poll = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        self.pollen = pollen
        self.last_poll = last_poll
        _LOGGER.debug("Pollen fetched")

        return {"success": True}

    async def async_force_poll(self) -> dict:
        """Handle the action call to force poll the API."""
        _LOGGER.info("Force poll action called")
        await self.async_refresh()
        if not self.last_update_success:
            raise HomeAssistantError("Failed to refresh Pollen.lu data") from self.last_exception
        return {"success": True}
