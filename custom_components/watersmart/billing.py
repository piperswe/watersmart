"""Parse billed line items from WaterSmart's billing charts."""

from dataclasses import dataclass
import datetime as dt
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class Bill:
    """The sum of the line items on one bill date."""

    date: dt.date
    amount: float


@dataclass(frozen=True)
class BillingChart:
    """A separate billed service, such as water or wastewater."""

    chart_id: int
    name: str
    currency: str
    bills: tuple[Bill, ...]

    @property
    def key(self) -> str:
        """A stable key that never mixes currencies or billed services."""
        return f"billed_cost_{self.chart_id}_{self.currency.lower()}"


def parse_billing_chart(payload: dict[str, Any], currency: str) -> BillingChart:
    """Sum fixed and variable line items by bill date.

    A missing or null line item makes the history incomplete; it is not zero.
    Forecasts and comparative charts are not part of this response.

    Returns:
        Billed amounts in chronological order.

    Raises:
        ValueError: If the chart is not monetary or contains invalid charges.
    """
    chart = payload["data"]["chartData"]
    if chart["units"] != "dollars":
        message = "Unsupported billing chart units"
        raise ValueError(message)
    totals: dict[dt.date, Decimal] = {}
    all_dates: set[dt.date] = set()
    complete_dates: set[dt.date] | None = None
    for group in chart["data_series"].values():
        for item in group.values():
            dates: set[dt.date] = set()
            for point in item["series"]:
                date = dt.date.fromisoformat(point["x"])
                if date in dates:
                    message = "Duplicate billing line item date"
                    raise ValueError(message)
                dates.add(date)
                all_dates.add(date)
                if point["y"] is None:
                    continue
                amount = Decimal(str(point["y"]))
                if not amount.is_finite():
                    message = "Non-finite billing charge"
                    raise ValueError(message)
                totals[date] = totals.get(date, Decimal(0)) + amount
            valid_dates = {
                dt.date.fromisoformat(point["x"])
                for point in item["series"]
                if point["y"] is not None
            }
            complete_dates = (
                valid_dates if complete_dates is None else complete_dates & valid_dates
            )
    if all_dates != (complete_dates or set()):
        message = "Incomplete billing line items"
        raise ValueError(message)
    return BillingChart(
        chart_id=int(chart["ledger_bill_chart_id"]),
        name=chart["commentary"]["chart_header"].strip(),
        currency=currency,
        bills=tuple(
            Bill(date=date, amount=float(totals[date]))
            for date in sorted(complete_dates or ())
        ),
    )
