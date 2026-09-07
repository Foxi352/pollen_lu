from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify
import logging
import math
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def _dict_items(value):
    """Ignore malformed entries in optional API lists."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the sensor platform."""
    _LOGGER.debug("async_setup_entry()")
    coordinator = hass.data[DOMAIN][entry.entry_id]
    sensors = []
    for pollen in _dict_items(coordinator.pollen):
        if (
            not isinstance(pollen.get("translationKey"), str)
            or not pollen["translationKey"]
            or pollen.get("id") is None
        ):
            _LOGGER.warning("Skipping pollen record without an ID or translation key")
            continue
        if pollen.get("active"):
            sensors.append(PollenSensor(coordinator, pollen))
    async_add_entities(sensors)

class PollenSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    
    def __init__(self, coordinator, pollen):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_type = pollen.get("translationKey")
        self._attr_unique_id = f"{pollen.get('id')}_{self.entity_type}"
        self.entity_id = f"sensor.{slugify(f'pollen_{self.entity_type}')}"
        self._attr_native_unit_of_measurement = "p/m³"
        self._attr_icon = "mdi:flower-pollen"
        self._attr_device_class = None

    def translate(self, key, domain):
        if not isinstance(key, str) or not key:
            return None
        language = self.hass.config.language
        for item in _dict_items(self.coordinator.translations):
            if item.get("key") != key or item.get("domain") != domain:
                continue
            for translation in _dict_items(item.get("translations")):
                content = translation.get("content")
                if (
                    translation.get("locale") == language
                    and isinstance(content, str)
                    and content
                ):
                    return content
        return key

    @property
    def _pollen(self):
        """Find this pollen in the latest response, including inactive records."""
        return next(
            (item for item in _dict_items(self.coordinator.pollen)
             if item.get("translationKey") == self.entity_type),
            None,
        )

    @property
    def available(self):
        pollen = self._pollen
        return super().available and pollen is not None and bool(pollen.get("active"))

    @property
    def entity_picture(self):
        pollen = self._pollen or {}
        return next(
            (item["path"] for item in _dict_items(pollen.get("pictures"))
             if isinstance(item.get("path"), str) and item["path"]),
            None,
        )

    @property
    def name(self):
        """Name of the entity."""
        return f"Pollen {self.translate(self.entity_type, 'pollen')}"

    @property
    def native_value(self):
        """Return a count, or unknown when no usable measurement is provided."""
        pollen = self._pollen
        if pollen is None or not pollen.get("active"):
            return None
        if pollen.get("level") == "undetected":
            return 0
        value = pollen.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return None
        try:
            value = float(value)
        except (ValueError, OverflowError):
            return None
        if not math.isfinite(value) or value < 0:
            return None
        return round(value)

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        attributes = {
            "last_poll": self.coordinator.last_poll,
            "next_poll": self.coordinator.next_poll,
        }
        pollen = self._pollen
        if pollen is None or not pollen.get("active"):
            return attributes
        attributes["level"] = pollen.get("level")
        attributes["last_update"] = pollen.get("lastMeasurementDate")
        descriptions = pollen.get("descriptions")
        if isinstance(descriptions, list):
            key = next((item for item in descriptions if isinstance(item, str) and item), None)
            if key:
                attributes["description"] = self.translate(key, "pollen")
        for threshold in _dict_items(pollen.get("threshold")):
            if threshold.get("type") == "medium":
                attribute = "moderate_threshold"
            elif threshold.get("type") == "high":
                attribute = "high_threshold"
            else:
                continue
            minimum = threshold.get("min")
            if (
                type(minimum) in (int, float)
                and minimum >= 0
                and (type(minimum) is int or math.isfinite(minimum))
            ):
                attributes[attribute] = minimum
        return attributes
