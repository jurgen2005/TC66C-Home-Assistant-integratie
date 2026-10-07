import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def mock_bt(enable_bluetooth):
    yield


@pytest.fixture(autouse=True)
def meter_heard_recently():
    """Default: the meter was just heard (otherwise the integration deliberately does not connect)."""
    import time
    from unittest.mock import patch
    with patch(
        "custom_components.tc66c.coordinator.TC66CCoordinator._refresh_last_seen",
        lambda self: time.monotonic() + 1e6,
    ) as p:
        yield p
