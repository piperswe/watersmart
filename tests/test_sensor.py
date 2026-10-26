"""Test sensor for simple integration."""

import datetime as dt

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.util.dt import utcnow
from homeassistant.util.unit_conversion import VolumeConverter
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from syrupy.assertion import SnapshotAssertion


@pytest.mark.usefixtures("init_integration")
def test_water_consumption_sensor(hass: HomeAssistant, mock_watersmart_client):
    """Expose a cumulative volume eligible for the Energy water-source picker."""
    state = hass.states.get("sensor.watersmart_test_water_consumption")
    records = mock_watersmart_client.async_get_hourly_data.return_value
    assert VolumeConverter.convert(
        float(state.state),
        state.attributes["unit_of_measurement"],
        UnitOfVolume.GALLONS,
    ) == pytest.approx(sum(r["gallons"] or 0 for r in records), rel=0.001)
    assert state.attributes["device_class"] == SensorDeviceClass.WATER
    assert state.attributes["state_class"] == SensorStateClass.TOTAL
    assert state.attributes["unit_of_measurement"] in {
        UnitOfVolume.LITERS,
        UnitOfVolume.GALLONS,
    }
    for period in ("hour", "full_day"):
        summary = hass.states.get(
            f"sensor.watersmart_test_gallons_for_most_recent_{period}"
        )
        assert "state_class" not in summary.attributes


@pytest.fixture
def client_hourly_data_full_day(mock_watersmart_client):
    hourly = mock_watersmart_client.async_get_hourly_data.return_value

    for gallons in range(25):
        last_time = dt.datetime.fromtimestamp(hourly[-1]["read_datetime"])  # noqa: DTZ006
        next_time = int((last_time + dt.timedelta(hours=1)).timestamp())
        hourly.append(
            dict(
                hourly[-1],
                read_datetime=next_time,
                gallons=float(gallons),
            )
        )

    mock_watersmart_client.async_get_hourly_data.return_value = hourly


@pytest.mark.usefixtures("client_hourly_data_full_day", "init_integration")
def test_most_recent_day_sensor(
    hass: HomeAssistant, mock_watersmart_client, snapshot: SnapshotAssertion
):
    """Test sensor."""
    recent_day_sensor_state = hass.states.get(
        "sensor.watersmart_test_gallons_for_most_recent_full_day"
    )

    assert snapshot == recent_day_sensor_state
    assert mock_watersmart_client.async_get_hourly_data.call_count == 1


@pytest.fixture
def client_hourly_data_recent_hour_higher(mock_watersmart_client):
    mock_watersmart_client.async_get_hourly_data.return_value[-1]["gallons"] = 14.3


@pytest.mark.usefixtures("client_hourly_data_recent_hour_higher", "init_integration")
def test_most_recent_hour_sensor(
    hass: HomeAssistant, mock_watersmart_client, snapshot: SnapshotAssertion
):
    """Test sensor."""
    recent_hour_sensor_state = hass.states.get(
        "sensor.watersmart_test_gallons_for_most_recent_hour"
    )

    assert snapshot == recent_hour_sensor_state
    assert mock_watersmart_client.async_get_hourly_data.call_count == 1


@pytest.fixture
def client_hourly_data_full_day_with_none(
    mock_watersmart_client, client_hourly_data_full_day
):
    mock_watersmart_client.async_get_hourly_data.return_value[-4]["gallons"] = None


@pytest.mark.usefixtures("client_hourly_data_full_day_with_none", "init_integration")
def test_most_recent_hour_sensor_with_none_in_data(
    hass: HomeAssistant, mock_watersmart_client, snapshot: SnapshotAssertion
):
    """Test sensor."""
    recent_day_sensor_state = hass.states.get(
        "sensor.watersmart_test_gallons_for_most_recent_full_day"
    )

    assert snapshot == recent_day_sensor_state
    assert mock_watersmart_client.async_get_hourly_data.call_count == 1


@pytest.mark.usefixtures("init_integration")
def test_sensors_for_zero_gallons(
    hass: HomeAssistant, mock_watersmart_client, snapshot: SnapshotAssertion
):
    """Test sensor."""
    recent_day_sensor_state = hass.states.get(
        "sensor.watersmart_test_gallons_for_most_recent_full_day"
    )

    assert snapshot == recent_day_sensor_state

    recent_hour_sensor_state = hass.states.get(
        "sensor.watersmart_test_gallons_for_most_recent_hour"
    )

    assert snapshot == recent_hour_sensor_state

    assert mock_watersmart_client.async_get_hourly_data.call_count == 1


@pytest.mark.usefixtures("init_integration")
async def test_sensor_update(hass: HomeAssistant, mock_watersmart_client):
    """Test sensor."""
    assert mock_watersmart_client.async_get_hourly_data.call_count == 1

    async_fire_time_changed(hass, utcnow() + dt.timedelta(minutes=10))
    await hass.async_block_till_done()

    assert mock_watersmart_client.async_get_hourly_data.call_count == 1

    async_fire_time_changed(hass, utcnow() + dt.timedelta(hours=1))
    await hass.async_block_till_done()

    assert mock_watersmart_client.async_get_hourly_data.call_count == 2


@pytest.mark.usefixtures("client_authentication_error", "init_integration")
def test_sensor_update_failure(hass: HomeAssistant, mock_watersmart_client):
    """Test sensor."""
    recent_day_sensor_state = hass.states.get(
        "sensor.watersmart_test_gallons_for_most_recent_full_day"
    )

    assert recent_day_sensor_state is None

    recent_hour_sensor_state = hass.states.get(
        "sensor.watersmart_test_gallons_for_most_recent_hour"
    )

    assert recent_hour_sensor_state is None

    assert mock_watersmart_client.async_get_hourly_data.call_count == 1
