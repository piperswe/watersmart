"""Billed cost parsing, portal discovery, and recorder integration."""

import copy
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import StatisticsMeta
from homeassistant.components.recorder.statistics import get_metadata
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util.dt import get_default_time_zone
import pytest

from custom_components.watersmart.billing import Bill, BillingChart, parse_billing_chart
from custom_components.watersmart.client import ScrapeError, WaterSmartClient
from custom_components.watersmart.statistics import billing_metadata_for

from .test_statistics import _coordinator, _Recorder, _refresh, _stat_row


@pytest.fixture
def chart(fixture_loader):
    return parse_billing_chart(fixture_loader.billing_response_obj, "USD")


def test_parse_billed_line_items(chart):
    """Sum fixed charges, surcharges, and volume charges exactly by bill date."""
    assert chart.bills == (
        Bill(dt.date(2026, 8, 27), 10.0),
        Bill(dt.date(2026, 9, 27), 19.0),
    )
    assert chart.key == "billed_cost_1_usd"
    metadata = billing_metadata_for("ENTRY", "test", chart)
    assert metadata["statistic_id"] == "watersmart:entry_billed_cost_1_usd"
    assert metadata["has_sum"] is True
    assert metadata["unit_of_measurement"] == "USD"
    assert "unit_class" not in metadata
    assert StatisticsMeta.from_meta(metadata).has_sum is True


@pytest.mark.parametrize("missing", [None, "absent"])
def test_incomplete_bill_is_not_zero(fixture_loader, missing):
    payload = fixture_loader.billing_response_obj
    points = payload["data"]["chartData"]["data_series"]["variable"]["Tier I Charge"][
        "series"
    ]
    if missing is None:
        points[0]["y"] = None
    else:
        points.pop(0)
    with pytest.raises(ValueError, match="Incomplete billing"):
        parse_billing_chart(payload, "USD")


@pytest.mark.parametrize("invalid", ["duplicate", "infinite", "units"])
def test_invalid_bills_rejected(fixture_loader, invalid):
    payload = fixture_loader.billing_response_obj
    data = payload["data"]["chartData"]
    points = data["data_series"]["variable"]["Tier I Charge"]["series"]
    if invalid == "duplicate":
        points.append(points[0])
    elif invalid == "infinite":
        points[0]["y"] = "NaN"
    else:
        data["units"] = "gallons"
    with pytest.raises(ValueError, match=r"Duplicate|Non-finite|Unsupported"):
        parse_billing_chart(payload, "USD")


def test_credits_and_empty_charts(fixture_loader):
    payload = fixture_loader.billing_response_obj
    data = payload["data"]["chartData"]
    data["data_series"]["variable"]["Tier I Charge"]["series"][0]["y"] = -20
    assert parse_billing_chart(payload, "USD").bills[-1].amount == -11.95
    data["data_series"] = {}
    assert parse_billing_chart(payload, "USD").bills == ()


@pytest.fixture
async def billing_client(hass, mock_aiohttp_session, fixture_loader):
    mock_aiohttp_session.post.return_value.text.return_value = (
        fixture_loader.login_success_html
    )
    client = WaterSmartClient(hostname="test", username="", password="")
    await client.async_get_account_number()
    return client


def _response(*, text="", data=None):
    return SimpleNamespace(
        text=AsyncMock(return_value=text),
        json=AsyncMock(return_value=data),
        raise_for_status=MagicMock(),
    )


async def test_discover_separate_billed_services(
    billing_client, mock_aiohttp_session, fixture_loader
):
    water = fixture_loader.billing_response_obj
    wastewater = copy.deepcopy(water)
    wastewater["data"]["chartData"]["ledger_bill_chart_id"] = 2
    wastewater["data"]["chartData"]["commentary"]["chart_header"] = "Wastewater charges"
    mock_aiohttp_session.get.side_effect = [
        _response(text=fixture_loader.billing_page_html),
        _response(data=water),
        _response(data=wastewater),
    ]
    charts = await billing_client.async_get_billing_data()
    assert [c.chart_id for c in charts] == [1, 2]
    assert charts[0].key != charts[1].key
    requests = mock_aiohttp_session.get.call_args_list[-3:]
    assert requests[0].args[0].endswith("/index.php/billing/evaluate")
    assert requests[1].args[0].endswith("chartID=1&module=portal&commentary=full")
    assert requests[2].kwargs["headers"]["User-Agent"].startswith("Mozilla/5.0")


@pytest.mark.parametrize("invalid", ["country", "currency", "origin", "path", "id"])
async def test_reject_unsupported_billing_sources(
    billing_client, mock_aiohttp_session, fixture_loader, invalid
):
    page = fixture_loader.billing_page_html
    payload = fixture_loader.billing_response_obj
    if invalid == "country":
        page = page.replace('code="US"', 'code="CA"')
    elif invalid == "currency":
        page = page.replace('symbol="$"', 'symbol="EUR"')
    elif invalid == "origin":
        page = page.replace('data-url="/', 'data-url="https://example.com/')
    elif invalid == "path":
        page = page.replace("billLineItemChart?", "OtherChart?")
    else:
        payload["data"]["chartData"]["ledger_bill_chart_id"] = 999
    mock_aiohttp_session.get.side_effect = [
        _response(text=page),
        _response(data=payload),
    ]
    with pytest.raises(ScrapeError):
        await billing_client.async_get_billing_data()


async def test_portal_without_billing(billing_client, mock_aiohttp_session):
    mock_aiohttp_session.get.side_effect = [_response(text="<html></html>")]
    assert await billing_client.async_get_billing_data() == []


async def test_billing_import_and_late_sensor_discovery(
    hass: HomeAssistant, mock_watersmart_client, init_integration, chart
):
    """Discover costs after setup and import each bill only once on refresh."""
    mock_watersmart_client.async_get_billing_data.return_value = [chart]
    with _Recorder() as recorder:
        await _refresh(hass)
        await _refresh(hass)
    assert recorder.add_external.call_count == 4  # usage and cost each refresh
    metadata = recorder.add_external.call_args.args[1]
    rows = recorder.imported_rows
    assert metadata["unit_of_measurement"] == "USD"
    assert [r["sum"] for r in rows] == [10, 29]
    state = hass.states.get("sensor.watersmart_test_billed_cost")
    assert float(state.state) == 29
    assert state.attributes["state_class"] == "total"
    assert state.attributes["device_class"] == "monetary"
    assert state.attributes["unit_of_measurement"] == "USD"
    assert state.attributes["last_bill_amount"] == 19
    assert state.attributes["last_bill_date"] == "2026-09-27"
    registry = er.async_get(hass)
    entry = registry.async_get(state.entity_id)
    assert entry.unique_id == f"{init_integration.entry_id}-{chart.key}".lower()
    mock_watersmart_client.async_get_billing_data.side_effect = RuntimeError(
        "billing down"
    )
    with _Recorder():
        await _refresh(hass)
    assert hass.states.get(state.entity_id).state == "unknown"
    assert _coordinator(hass).last_update_success


async def test_billing_anchor_preserves_expired_history_and_applies_credit(
    hass, mock_watersmart_client, init_integration, chart
):
    mock_watersmart_client.async_get_billing_data.return_value = [
        BillingChart(
            chart.chart_id, chart.name, "USD", (Bill(dt.date(2026, 9, 27), -5),)
        )
    ]
    start = dt.datetime(2026, 8, 27, tzinfo=get_default_time_zone()).astimezone(dt.UTC)
    metadata = billing_metadata_for(init_integration.entry_id, "test", chart)
    with _Recorder(
        during_period={metadata["statistic_id"]: [_stat_row(start, sum=100)]}
    ) as recorder:
        await _refresh(hass)
    assert recorder.imported_rows[-1]["sum"] == 95
    query = recorder.stats_during.call_args
    assert query.kwargs["units"] is None
    assert query.args[2] == dt.datetime(
        2026, 9, 27, tzinfo=get_default_time_zone()
    ).astimezone(dt.UTC)


@pytest.mark.parametrize("failure", ["query", "write", "empty"])
async def test_billing_recorder_failures_do_not_break_water(
    hass, mock_watersmart_client, init_integration, chart, failure
):
    if failure == "empty":
        chart = BillingChart(chart.chart_id, chart.name, chart.currency, ())
    mock_watersmart_client.async_get_billing_data.return_value = [chart]
    kwargs = {}
    if failure == "write":
        kwargs["add_side_effect"] = RuntimeError("write failed")
    with _Recorder(**kwargs) as recorder:
        if failure == "query":
            recorder.stats_during.side_effect = RuntimeError("query failed")
        await _refresh(hass)
    assert _coordinator(hass).last_update_success
    if failure != "empty":
        assert hass.states.get("sensor.watersmart_test_billed_cost").state == "unknown"


async def test_multiple_cost_sensors_remain_separate(
    hass, mock_watersmart_client, init_integration, chart
):
    """Water and wastewater get distinct entities, totals, and statistic ids."""
    wastewater = BillingChart(
        chart_id=2,
        name="Wastewater charges",
        currency="USD",
        bills=(Bill(dt.date(2026, 9, 27), 75.5),),
    )
    mock_watersmart_client.async_get_billing_data.return_value = [chart, wastewater]
    await _refresh(hass)
    recorder = get_instance(hass)
    await recorder.async_block_till_done()
    assert hass.states.get("sensor.watersmart_test_billed_cost").state == "29.0"
    assert hass.states.get("sensor.watersmart_test_billed_cost_2").state == "75.5"
    metadata = await recorder.async_add_executor_job(
        lambda: get_metadata(hass, statistic_source="watersmart")
    )
    costs = [meta for key, (_id, meta) in metadata.items() if "_billed_cost_" in key]
    assert len(costs) == 2
    assert all(meta["unit_of_measurement"] == "USD" for meta in costs)
    # Removal must find both cost series, even after the portal disappears.
    await hass.async_block_till_done()
    await hass.config_entries.async_remove(init_integration.entry_id)
    await hass.async_block_till_done()
    await recorder.async_block_till_done()
    assert not await recorder.async_add_executor_job(
        lambda: get_metadata(hass, statistic_source="watersmart")
    )
