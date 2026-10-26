"""WaterSmart client to connect & scrape data."""

from __future__ import annotations

from collections.abc import Callable
import contextlib
import datetime as dt
import functools
import re
from typing import Any, TypedDict, cast

import aiohttp
from bs4 import BeautifulSoup, PageElement
from yarl import URL

from .billing import BillingChart, parse_billing_chart

# Account number format will vary between municipality, so
# match on a string of non-whitespace characters.
ACCOUNT_NUMBER_RE = re.compile(r"^\S+$")

# Some WaterSmart portals (and the WAFs/CDNs in front of them) reject or serve
# different content to non-browser HTTP clients. Home Assistant's shared aiohttp
# session sends a non-browser User-Agent, which causes the login POST to fail
# authentication even with valid credentials. Send browser-like headers on the
# requests this client makes so the authenticated flow looks like a browser.
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _authenticated[F: Callable[..., Any], ReturnT](func: F) -> F:
    @functools.wraps(func)
    async def _pre_authenticate(
        self: WaterSmartClient,
        *args,  # noqa: ANN002
        **kwargs,  # noqa: ANN003
    ) -> ReturnT:
        await self._authenticate_if_needed()
        return cast("ReturnT", await func(self, *args, **kwargs))

    return cast("F", _pre_authenticate)


class AuthenticationError(Exception):
    """Authentication Error."""

    def __init__(self, errors: list[str] | None = None) -> None:
        """Initialize."""
        self._errors = errors


class InvalidAccountNumberError(Exception):
    """Invalid account number Error."""


class ScrapeError(Exception):
    """Scrape Error."""


class Requires2FAError(Exception):
    """Error to indicate 2FA code is required."""


class UsageHistoryPayload(TypedDict):
    """UsageHistoryPayload class."""

    data: UsageHistory


class UsageHistory(TypedDict):
    """UsageHistory class."""

    series: list[UsageRecord]


class UsageRecord(TypedDict):
    """UsageRecord class."""

    read_datetime: int
    gallons: float | None
    leak_gallons: int | None
    flags: None


class WaterSmartClient:
    """WaterSmart Client."""

    def __init__(
        self,
        hostname: str,
        username: str,
        password: str,
        session: aiohttp.ClientSession = None,
        cookies: dict[str, str] | None = None,
    ) -> None:
        """Initialize."""
        self._hostname = hostname
        self._username = username
        self._password = password
        self._session = session or aiohttp.ClientSession()
        self._account_number: str | None = None
        self._authenticated_at: dt.datetime | None = None

        if cookies:
            domain_url = URL(f"https://{self._hostname}.watersmart.com")
            self._session.cookie_jar.update_cookies(cookies, response_url=domain_url)

    @_authenticated
    async def async_get_account_number(self) -> str | None:
        """Authenticate the client.

        Returns:
            The account number.
        """

        return self._account_number

    @_authenticated
    async def async_get_hourly_data(self) -> list[UsageRecord]:
        """Get hourly water usage data.

        Returns:
            The objects in the response data.
        """

        session = self._session
        hostname = self._hostname
        response = await session.get(
            f"https://{hostname}.watersmart.com/index.php/rest/v1/Chart/RealTimeChart",
            headers=_DEFAULT_HEADERS,
        )
        response_json: UsageHistoryPayload = await response.json()

        return response_json["data"]["series"]

    @_authenticated
    async def async_get_billing_data(self) -> list[BillingChart]:
        """Discover billed-service charts and retrieve their line items.

        Returns:
            Separate billing histories, or an empty list on portals without charts.

        Raises:
            ScrapeError: If the portal currency or chart URL is unsupported.
        """
        base = URL(f"https://{self._hostname}.watersmart.com")
        response = await self._session.get(
            str(base / "index.php/billing/evaluate"), headers=_DEFAULT_HEADERS
        )
        response.raise_for_status()
        soup = BeautifulSoup(await response.text(), "html.parser")
        nodes = soup.select(".bill-line-item-chart[data-url]")
        if not nodes:
            return []
        country = soup.select_one("#service-country-data")
        currency = soup.select_one("#currency-data")
        # The chart says only 'dollars'. Confirm USD from portal metadata,
        # rather than labeling another country's dollars as US dollars.
        if (
            country is None
            or country.get("data-service-country-code") != "US"
            or currency is None
            or currency.get("data-currency-symbol") != "$"
        ):
            message = "Unsupported or missing billing currency"
            raise ScrapeError(message)
        charts: dict[int, BillingChart] = {}
        for node in nodes:
            url = base.join(URL(str(node["data-url"])))
            if (
                url.origin() != base
                or url.path != "/index.php/rest/v1/Chart/billLineItemChart"
            ):
                message = "Unexpected billing chart URL"
                raise ScrapeError(message)
            response = await self._session.get(str(url), headers=_DEFAULT_HEADERS)
            response.raise_for_status()
            chart = parse_billing_chart(await response.json(), "USD")
            if str(chart.chart_id) != url.query.get("chartID"):
                message = "Billing chart ID does not match request"
                raise ScrapeError(message)
            charts[chart.chart_id] = chart
        return list(charts.values())

    async def _authenticate_if_needed(self) -> None:
        if not self._authenticated_at or self._authenticated_at < dt.datetime.now(
            tz=dt.UTC
        ) - dt.timedelta(minutes=10):
            await self._authenticate()
        self._authenticated_at = dt.datetime.now(tz=dt.UTC)

    async def _authenticate(self) -> None:
        session = self._session
        hostname = self._hostname

        with contextlib.suppress(aiohttp.ClientError):
            await session.get(
                f"https://{hostname}.watersmart.com/index.php/logout/login",
                headers=_DEFAULT_HEADERS,
            )

        login_response = await session.post(
            f"https://{hostname}.watersmart.com/index.php/logout/login?forceEmail=1",
            data={
                "token": "",
                "email": self._username,
                "password": self._password,
            },
            headers=_DEFAULT_HEADERS,
        )
        login_response_text = await login_response.text()
        soup = BeautifulSoup(login_response_text, "html.parser")

        if (
            "verification code" in login_response_text.lower()
            or "two-factor" in login_response_text.lower()
        ):
            raise Requires2FAError

        login_refresh_token_node = soup.find("input", {"name": "loginRefreshToken"})
        login_refresh_token = (
            login_refresh_token_node.get("value", "")
            if login_refresh_token_node
            else None
        )

        if login_refresh_token:
            login_response = await session.post(
                f"https://{hostname}.watersmart.com/index.php/logout/login?forceEmail=1",
                data={
                    "token": "",
                    "loginRefreshToken": login_refresh_token,
                    "email": self._username,
                    "password": self._password,
                },
                headers=_DEFAULT_HEADERS,
            )
            login_response_text = await login_response.text()
            soup = BeautifulSoup(login_response_text, "html.parser")

        errors = [error.text.strip() for error in soup.select(".error-message")]
        errors = [error for error in errors if error]

        if len(errors):
            raise AuthenticationError(errors)

        account = _assert_node(
            soup.find(id="account-navigation"), "Missing #account-navigation"
        )
        account_number_title = _assert_node(
            account.find(lambda node: node.get_text(strip=True) == "Account Number"),
            "Missing tag with string content `Account Number` under #account-navigation",
        )
        account_section = account_number_title.parent
        account_number_title.extract()

        account_number = account_section.text.strip()

        if not ACCOUNT_NUMBER_RE.match(account_number):
            self._account_number = None
            raise InvalidAccountNumberError("invalid account number: " + account_number)

        self._account_number = account_number

    async def async_verify_2fa(self, code: str) -> dict[str, str]:
        """Submit 2FA code and extract cookies.

        Returns:
            The extracted session cookies.

        Raises:
            AuthenticationError: If the 2FA code is rejected.
        """
        session = self._session
        hostname = self._hostname

        verify_response = await session.post(
            f"https://{hostname}.watersmart.com/index.php/welcome/verify",
            data={"verificationCode": code},
            headers=_DEFAULT_HEADERS,
        )

        text = await verify_response.text()

        soup = BeautifulSoup(text, "html.parser")
        errors = [err.text.strip() for err in soup.select(".error-message")]
        errors = [err for err in errors if err]

        if errors:
            raise AuthenticationError(errors)

        # Extract cookies from the session jar to save in Home Assistant
        cookies = {}
        for cookie in session.cookie_jar:
            cookies[cookie.key] = cookie.value

        return cookies


def _assert_node(node: PageElement, message: str) -> PageElement:
    if not node:
        raise ScrapeError(message)
    return node
