DOMAIN = "pollen_lu"
NAME = "Pollen.lu"

API_URL = "https://pollen-api.chl.lu/api"

DEFAULT_SCAN_INTERVAL = 60


def is_valid_scan_interval(value):
    """Polling intervals must be positive whole minutes, excluding booleans."""
    return type(value) is int and value > 0
