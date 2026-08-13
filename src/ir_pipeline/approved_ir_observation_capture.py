"""Fail-closed public-browser capture for approved Wix/Rubrik observations."""

from __future__ import annotations

import base64
import binascii
import hashlib
import importlib
import json
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime
from enum import StrEnum
from html.parser import HTMLParser
from typing import Any, Protocol, Self, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ir_pipeline._net import (
    PLAYWRIGHT_NETWORK_LOCKDOWN_ARG,
    PLAYWRIGHT_NO_PROXY_ARG,
    build_public_opener,
    install_public_only_playwright_routing,
)
from pipeline.approved_ir_catalog import IrCatalogError, build_catalog
from pipeline.approved_ir_rubrik import (
    RubrikLinkObservation,
    RubrikQuarterObservation,
    load_rubrik_row_observations,
    parse_rubrik_quarter_rows,
)
from pipeline.approved_ir_wix import (
    WixLinkObservation,
    WixPanelObservation,
    WixRenderedObservation,
    load_wix_rendered_observations,
    parse_wix_visible_quarters,
)
from pipeline.source_policy import AdapterKey, issuer_policy

_SCHEMA_VERSION = "approved-ir-observation-bundle@1"
_RENDERED_PROOF_SCHEMA_VERSION = "approved-ir-rendered-link-proof@1"
_MAX_WIRE_BYTES = 50_000_000
_MAX_AUTHORITY_BYTES = 10_000_000
_WIX_VISIBLE_EXPORT_SCHEMA_VERSION = "wix-visible-browser-export@1"
_WIX_DOCUMENT_TITLE = "Wix.com Investor Relations: Events & Presentations"
_WIX_IMPORT_PERIODS = (
    date(2026, 6, 30),
    date(2026, 3, 31),
    date(2025, 12, 31),
    date(2025, 9, 30),
    date(2025, 6, 30),
)
_RUBRIK_VISIBLE_EXPORT_SCHEMA_VERSION = "rubrik-visible-browser-export@1"
_RUBRIK_DOCUMENT_TITLE = "Rubrik - Financials - Quarterly Results"
_RUBRIK_IMPORT_PERIODS = (
    date(2026, 4, 30),
    date(2026, 1, 31),
    date(2025, 10, 31),
    date(2025, 7, 31),
    date(2025, 4, 30),
)
_CaptureT = TypeVar("_CaptureT")


class _PlaywrightResponse(Protocol):
    status: int


class _PlaywrightPage(Protocol):
    url: str

    def goto(self, url: str, *, wait_until: str, timeout: int) -> _PlaywrightResponse | None: ...

    def wait_for_timeout(self, timeout: int) -> None: ...


class _PlaywrightContext(Protocol):
    def new_page(self) -> _PlaywrightPage: ...


class _PlaywrightBrowser(Protocol):
    def new_context(self, *, user_agent: str, service_workers: str) -> _PlaywrightContext: ...

    def close(self) -> None: ...


class _ChromiumRuntime(Protocol):
    def launch(self, *, headless: bool, args: list[str]) -> _PlaywrightBrowser: ...


class _PlaywrightRuntime(Protocol):
    chromium: _ChromiumRuntime


class _PlaywrightManager(Protocol):
    def __enter__(self) -> _PlaywrightRuntime: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None: ...


class _SyncPlaywrightFactory(Protocol):
    def __call__(self) -> _PlaywrightManager: ...


class ApprovedIrObservationCaptureError(ValueError):
    """Public IR observation evidence could not be established safely."""


class ApprovedIrObservationAuthenticationError(ApprovedIrObservationCaptureError):
    """The publisher returned an authentication/authorization hard stop."""


class ObservationArtifactRole(StrEnum):
    AUTHORITY_RAW = "authority_raw"
    RENDERED_STATE = "rendered_state"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PublisherAuthorityResponse(_FrozenModel):
    requested_url: str
    final_url: str
    status_code: int = Field(ge=100, le=599)
    media_type: str = Field(min_length=1, max_length=255)
    content_bytes: bytes


class WixRenderedPanelCapture(_FrozenModel):
    observation_key: str
    authority_url: str
    requested_year: int
    selected_year: int
    year_control_locator: str
    requested_quarter_end: date
    panels: tuple[WixPanelObservation, ...]
    rendered_state_bytes: bytes
    evidence_locator: str


class RubrikRenderedRowCapture(_FrozenModel):
    observation_key: str
    authority_url: str
    quarter_end: date
    row_locator: str
    links: tuple[RubrikLinkObservation, ...]
    rendered_state_bytes: bytes


class ApprovedIrObservationCaptureRequest(_FrozenModel):
    issuer_identifier: str = Field(min_length=1, max_length=128)
    requested_quarter_ends: tuple[date, ...] = Field(min_length=1, max_length=20)
    captured_at: datetime
    user_agent: str = Field(min_length=8, max_length=512)
    timeout_ms: int = Field(default=30_000, ge=1_000, le=300_000)

    @field_validator("captured_at")
    @classmethod
    def _naive_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is not None:
            raise ValueError("captured_at must use the repository naive-UTC convention")
        return value

    @field_validator("requested_quarter_ends")
    @classmethod
    def _unique_periods(cls, value: tuple[date, ...]) -> tuple[date, ...]:
        if len(value) != len(set(value)):
            raise ValueError("requested reporting periods must be unique")
        return value


class _WixVisibleExportLink(_FrozenModel):
    title: str = Field(min_length=1, max_length=255)
    url: str = Field(min_length=1, max_length=4_096)


class _WixVisibleExportState(_FrozenModel):
    container_html: str = Field(alias="containerHtml", min_length=1, max_length=5_000_000)
    container_id: str = Field(alias="containerId", min_length=1, max_length=255)
    heading: str = Field(min_length=1, max_length=255)
    links: tuple[_WixVisibleExportLink, ...] = Field(min_length=1, max_length=20)
    period_end: date = Field(alias="periodEnd")
    quarter_name: str = Field(alias="quarterName", min_length=1, max_length=64)
    requested_year: int = Field(alias="requestedYear", ge=2000, le=2100)
    selected_year: int = Field(alias="selectedYear", ge=2000, le=2100)


class _WixVisibleBrowserExport(_FrozenModel):
    schema_version: str
    authority_url: str
    captured_at: datetime
    document_title: str
    states: tuple[_WixVisibleExportState, ...] = Field(min_length=1, max_length=20)


class _RubrikVisibleExportLink(_FrozenModel):
    text: str = Field(min_length=1, max_length=512)
    href: str = Field(min_length=1, max_length=4_096)
    aria_label: str | None = Field(alias="ariaLabel", default=None, max_length=512)


class _RubrikVisibleExportState(_FrozenModel):
    period_end: date = Field(alias="periodEnd")
    selected_label: str = Field(alias="selectedLabel", min_length=1, max_length=64)
    tab_outer_html: str | None = Field(alias="tabOuterHtml", default=None, max_length=100_000)
    container_html: str = Field(alias="containerHtml", min_length=1, max_length=5_000_000)
    links: tuple[_RubrikVisibleExportLink, ...] = Field(min_length=1, max_length=100)


class _RubrikVisibleBrowserExport(_FrozenModel):
    schema_version: str = Field(alias="schema")
    issuer_identifier: str = Field(alias="issuerIdentifier")
    authority_url: str = Field(alias="authorityUrl")
    document_title: str = Field(alias="documentTitle")
    captured_at: datetime = Field(alias="capturedAt")
    states: tuple[_RubrikVisibleExportState, ...] = Field(min_length=1, max_length=20)


class ApprovedIrBrowser(Protocol):
    def fetch_authority(
        self, authority_url: str, *, user_agent: str, timeout_ms: int
    ) -> PublisherAuthorityResponse: ...

    def capture_wix_year(
        self,
        authority_url: str,
        *,
        requested_year: int,
        requested_quarter_ends: tuple[date, ...],
        user_agent: str,
        timeout_ms: int,
    ) -> tuple[WixRenderedPanelCapture, ...]: ...

    def capture_rubrik_rows(
        self,
        authority_url: str,
        *,
        requested_quarter_ends: tuple[date, ...],
        user_agent: str,
        timeout_ms: int,
    ) -> tuple[RubrikRenderedRowCapture, ...]: ...


class PlaywrightApprovedIrBrowser:
    """Public-only browser adapter that consumes visible selected states only."""

    def fetch_authority(
        self, authority_url: str, *, user_agent: str, timeout_ms: int
    ) -> PublisherAuthorityResponse:
        request = urllib.request.Request(
            authority_url,
            headers={"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml"},
        )
        try:
            with build_public_opener().open(
                request, timeout=min(max(timeout_ms / 1000, 1), 30)
            ) as response:
                body = response.read(_MAX_AUTHORITY_BYTES + 1)
                if len(body) > _MAX_AUTHORITY_BYTES:
                    raise ApprovedIrObservationCaptureError(
                        "publisher authority response exceeds the byte limit"
                    )
                return PublisherAuthorityResponse(
                    requested_url=authority_url,
                    final_url=str(response.url),
                    status_code=int(response.status),
                    media_type=str(response.headers.get("Content-Type", "text/html")),
                    content_bytes=body,
                )
        except urllib.error.HTTPError as exc:
            return PublisherAuthorityResponse(
                requested_url=authority_url,
                final_url=str(exc.url),
                status_code=exc.code,
                media_type=str(exc.headers.get("Content-Type", "text/html")),
                content_bytes=exc.read(_MAX_AUTHORITY_BYTES + 1),
            )
        except (OSError, urllib.error.URLError) as exc:
            raise ApprovedIrObservationCaptureError(
                f"publisher authority fetch failed: {type(exc).__name__}"
            ) from None

    def capture_wix_year(
        self,
        authority_url: str,
        *,
        requested_year: int,
        requested_quarter_ends: tuple[date, ...],
        user_agent: str,
        timeout_ms: int,
    ) -> tuple[WixRenderedPanelCapture, ...]:
        return tuple(
            self._with_page(
                authority_url,
                user_agent,
                timeout_ms,
                lambda page, period=period: _capture_wix_period(
                    page, authority_url, requested_year, period
                ),
            )
            for period in requested_quarter_ends
        )

    def capture_rubrik_rows(
        self,
        authority_url: str,
        *,
        requested_quarter_ends: tuple[date, ...],
        user_agent: str,
        timeout_ms: int,
    ) -> tuple[RubrikRenderedRowCapture, ...]:
        return tuple(
            self._with_page(
                authority_url,
                user_agent,
                timeout_ms,
                lambda page, period=period: _capture_rubrik_period(page, authority_url, period),
            )
            for period in requested_quarter_ends
        )

    @staticmethod
    def _with_page(
        authority_url: str,
        user_agent: str,
        timeout_ms: int,
        capture: Callable[[Any], _CaptureT],
    ) -> _CaptureT:
        module = importlib.import_module("playwright.sync_api")
        factory_value = getattr(module, "sync_playwright", None)
        if not callable(factory_value):
            raise ApprovedIrObservationCaptureError(
                "Playwright sync browser runtime is unavailable"
            )
        sync_playwright = cast(_SyncPlaywrightFactory, factory_value)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-http2",
                    PLAYWRIGHT_NETWORK_LOCKDOWN_ARG,
                    PLAYWRIGHT_NO_PROXY_ARG,
                ],
            )
            try:
                context = browser.new_context(user_agent=user_agent, service_workers="block")
                install_public_only_playwright_routing(context, timeout_s=timeout_ms / 1000)
                page = context.new_page()
                response = page.goto(
                    authority_url,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
                status = None if response is None else response.status
                if status in {401, 403}:
                    raise ApprovedIrObservationAuthenticationError(
                        "publisher authority requires authentication or authorization"
                    )
                if status != 200 or page.url != authority_url:
                    raise ApprovedIrObservationCaptureError(
                        "rendered publisher authority redirected or returned a non-success status"
                    )
                page.wait_for_timeout(1_000)
                try:
                    return capture(page)
                except ApprovedIrObservationCaptureError:
                    raise
                except Exception as exc:
                    raise ApprovedIrObservationCaptureError(
                        f"rendered publisher capture failed: {type(exc).__name__}"
                    ) from None
            finally:
                browser.close()


class _VisibleAnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[tuple[str, str, bool]] = []
        self.detailed_anchors: list[tuple[str, str | None, str, bool]] = []
        self.element_ids: set[str] = set()
        self._active: dict[str, object] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value or "" for key, value in attrs}
        if values.get("id"):
            self.element_ids.add(values["id"])
        if tag.casefold() != "a" or self._active is not None:
            return
        style = values.get("style", "").replace(" ", "").casefold()
        hidden = (
            "hidden" in values
            or values.get("aria-hidden", "").casefold() == "true"
            or "display:none" in style
            or "visibility:hidden" in style
        )
        self._active = {
            "href": values.get("href", "").strip(),
            "aria_label": values.get("aria-label", "").strip(),
            "hidden": hidden,
            "text": [],
        }

    def handle_data(self, data: str) -> None:
        if self._active is not None:
            cast("list[str]", self._active["text"]).append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() != "a" or self._active is None:
            return
        text = " ".join("".join(cast("list[str]", self._active["text"])).split())
        aria_label = str(self._active["aria_label"]) or None
        title = aria_label or text
        self.anchors.append((str(self._active["href"]), title, bool(self._active["hidden"])))
        self.detailed_anchors.append(
            (str(self._active["href"]), aria_label, text, bool(self._active["hidden"]))
        )
        self._active = None


def import_wix_visible_browser_export(export_bytes: bytes) -> ApprovedIrObservationBundle:
    """Validate an automatic visible-Chrome export and seal its exact proof bytes."""

    if not export_bytes or len(export_bytes) > _MAX_WIRE_BYTES:
        raise ApprovedIrObservationCaptureError("Wix visible-browser export size is invalid")
    try:
        exported = _WixVisibleBrowserExport.model_validate_json(export_bytes)
    except (ValueError, ValidationError) as exc:
        raise ApprovedIrObservationCaptureError(
            f"Wix visible-browser export schema is invalid: {exc}"
        ) from None
    policy = issuer_policy("WIX")
    if (
        exported.schema_version != _WIX_VISIBLE_EXPORT_SCHEMA_VERSION
        or exported.authority_url != policy.ir.authority_url
        or exported.document_title != _WIX_DOCUMENT_TITLE
    ):
        raise ApprovedIrObservationCaptureError(
            "Wix visible-browser export authority or document identity is invalid"
        )
    if len(exported.states) != len(_WIX_IMPORT_PERIODS) or {
        state.period_end for state in exported.states
    } != set(_WIX_IMPORT_PERIODS):
        raise ApprovedIrObservationCaptureError(
            "Wix visible-browser export does not contain the exact approved period set"
        )
    captured_at = exported.captured_at
    if captured_at.tzinfo is not None:
        captured_at = captured_at.astimezone(UTC).replace(tzinfo=None)
    captures: list[WixRenderedPanelCapture] = []
    for state in exported.states:
        captures.append(_wix_export_state_capture(exported, state))
    request = ApprovedIrObservationCaptureRequest(
        issuer_identifier="WIX",
        requested_quarter_ends=_WIX_IMPORT_PERIODS,
        captured_at=captured_at,
        user_agent="external-visible-browser-export",
    )
    browser = _ImportedWixBrowser(tuple(captures), export_bytes)
    return collect_approved_ir_observations(
        request,
        browser=browser,
        robots_allows=lambda _url, _agent: True,
    )


def import_rubrik_visible_browser_export(export_bytes: bytes) -> ApprovedIrObservationBundle:
    """Validate an automatic visible-Chrome Rubrik export and seal exact DOM proof."""

    if not export_bytes or len(export_bytes) > _MAX_WIRE_BYTES:
        raise ApprovedIrObservationCaptureError("Rubrik visible-browser export size is invalid")
    try:
        exported = _RubrikVisibleBrowserExport.model_validate_json(export_bytes)
    except (ValueError, ValidationError) as exc:
        raise ApprovedIrObservationCaptureError(
            f"Rubrik visible-browser export schema is invalid: {exc}"
        ) from None
    policy = issuer_policy("RBRK")
    if (
        exported.schema_version != _RUBRIK_VISIBLE_EXPORT_SCHEMA_VERSION
        or exported.issuer_identifier != "RBRK"
        or exported.authority_url != policy.ir.authority_url
        or exported.document_title != _RUBRIK_DOCUMENT_TITLE
    ):
        raise ApprovedIrObservationCaptureError(
            "Rubrik visible-browser export authority or document identity is invalid"
        )
    if len(exported.states) != len(_RUBRIK_IMPORT_PERIODS) or {
        state.period_end for state in exported.states
    } != set(_RUBRIK_IMPORT_PERIODS):
        raise ApprovedIrObservationCaptureError(
            "Rubrik visible-browser export does not contain the exact approved period set"
        )
    captured_at = exported.captured_at
    if captured_at.tzinfo is not None:
        captured_at = captured_at.astimezone(UTC).replace(tzinfo=None)
    captures = tuple(_rubrik_export_state_capture(exported, state) for state in exported.states)
    request = ApprovedIrObservationCaptureRequest(
        issuer_identifier="RBRK",
        requested_quarter_ends=_RUBRIK_IMPORT_PERIODS,
        captured_at=captured_at,
        user_agent="external-visible-browser-export",
    )
    return collect_approved_ir_observations(
        request,
        browser=_ImportedRubrikBrowser(captures, export_bytes),
        robots_allows=lambda _url, _agent: True,
    )


class _ImportedWixBrowser:
    def __init__(self, captures: tuple[WixRenderedPanelCapture, ...], export_bytes: bytes) -> None:
        self._captures = captures
        self._export_bytes = export_bytes

    def fetch_authority(
        self, authority_url: str, *, user_agent: str, timeout_ms: int
    ) -> PublisherAuthorityResponse:
        del user_agent, timeout_ms
        return PublisherAuthorityResponse(
            requested_url=authority_url,
            final_url=authority_url,
            status_code=200,
            media_type="application/json",
            content_bytes=self._export_bytes,
        )

    def capture_wix_year(
        self,
        authority_url: str,
        *,
        requested_year: int,
        requested_quarter_ends: tuple[date, ...],
        user_agent: str,
        timeout_ms: int,
    ) -> tuple[WixRenderedPanelCapture, ...]:
        del authority_url, user_agent, timeout_ms
        requested = set(requested_quarter_ends)
        return tuple(
            item
            for item in self._captures
            if item.requested_year == requested_year and item.requested_quarter_end in requested
        )

    def capture_rubrik_rows(
        self,
        authority_url: str,
        *,
        requested_quarter_ends: tuple[date, ...],
        user_agent: str,
        timeout_ms: int,
    ) -> tuple[RubrikRenderedRowCapture, ...]:
        del authority_url, requested_quarter_ends, user_agent, timeout_ms
        raise ApprovedIrObservationCaptureError("Wix export cannot supply Rubrik observations")


class _ImportedRubrikBrowser:
    def __init__(self, captures: tuple[RubrikRenderedRowCapture, ...], export_bytes: bytes) -> None:
        self._captures = captures
        self._export_bytes = export_bytes

    def fetch_authority(
        self, authority_url: str, *, user_agent: str, timeout_ms: int
    ) -> PublisherAuthorityResponse:
        del user_agent, timeout_ms
        return PublisherAuthorityResponse(
            requested_url=authority_url,
            final_url=authority_url,
            status_code=200,
            media_type="application/json",
            content_bytes=self._export_bytes,
        )

    def capture_wix_year(
        self,
        authority_url: str,
        *,
        requested_year: int,
        requested_quarter_ends: tuple[date, ...],
        user_agent: str,
        timeout_ms: int,
    ) -> tuple[WixRenderedPanelCapture, ...]:
        del authority_url, requested_year, requested_quarter_ends, user_agent, timeout_ms
        raise ApprovedIrObservationCaptureError("Rubrik export cannot supply Wix observations")

    def capture_rubrik_rows(
        self,
        authority_url: str,
        *,
        requested_quarter_ends: tuple[date, ...],
        user_agent: str,
        timeout_ms: int,
    ) -> tuple[RubrikRenderedRowCapture, ...]:
        del authority_url, user_agent, timeout_ms
        requested = set(requested_quarter_ends)
        return tuple(item for item in self._captures if item.quarter_end in requested)


def _rubrik_export_state_capture(
    exported: _RubrikVisibleBrowserExport,
    state: _RubrikVisibleExportState,
) -> RubrikRenderedRowCapture:
    expected_label = _rubrik_tab_label(state.period_end)
    if state.selected_label != expected_label:
        raise ApprovedIrObservationCaptureError(
            "Rubrik visible-browser export selected period identity is invalid"
        )
    if "[Truncated]" in state.container_html:
        raise ApprovedIrObservationCaptureError(
            "Rubrik visible-browser export container HTML is truncated"
        )
    if state.period_end != date(2025, 4, 30):
        tab_html = state.tab_outer_html or ""
        if (
            expected_label not in tab_html
            or 'aria-selected="true"' not in tab_html
            or 'role="tab"' not in tab_html
        ):
            raise ApprovedIrObservationCaptureError(
                "Rubrik visible-browser export selected tab is not proven"
            )
    parser = _VisibleAnchorParser()
    parser.feed(state.container_html)
    declared = [(item.href, item.aria_label, " ".join(item.text.split())) for item in state.links]
    if len(declared) != len(set(declared)):
        raise ApprovedIrObservationCaptureError(
            "Rubrik visible-browser export contains duplicate declared links"
        )
    parsed = [
        (href, aria_label, text)
        for href, aria_label, text, hidden in parser.detailed_anchors
        if not hidden
    ]
    if state.period_end == date(2025, 4, 30):
        links_proven = set(declared).issubset(set(parsed))
    else:
        links_proven = sorted(parsed) == sorted(declared)
    if not links_proven:
        raise ApprovedIrObservationCaptureError(
            "Rubrik visible-browser export links are not exactly proven by container HTML"
        )
    links: list[RubrikLinkObservation] = []
    for index, (href, aria_label, text) in enumerate(declared):
        title = aria_label or text
        declared_kind = _rubrik_export_kind(title)
        if declared_kind is None:
            continue
        links.append(
            RubrikLinkObservation(
                title=title,
                url=href,
                declared_kind=declared_kind,
                evidence_locator=(
                    f"visible-row[{expected_label}] > anchor[{index}]"
                    f"[text={json.dumps(title, ensure_ascii=True)}]"
                ),
            )
        )
    locator = f"visible-row[{expected_label}]"
    rendered_state = _canonical_json(
        {
            "schema_version": _RENDERED_PROOF_SCHEMA_VERSION,
            "page_url": exported.authority_url,
            "document_title": exported.document_title,
            "visible_state": {
                "selected_label": state.selected_label,
                "tab_outer_html": state.tab_outer_html,
                "container_outer_html": state.container_html,
                "declared_links": [item.model_dump(mode="json") for item in state.links],
                "links": [item.model_dump(mode="json") for item in links],
            },
        }
    ).encode("utf-8")
    return RubrikRenderedRowCapture(
        observation_key=f"rbrk-{state.period_end.isoformat()}",
        authority_url=exported.authority_url,
        quarter_end=state.period_end,
        row_locator=locator,
        links=tuple(links),
        rendered_state_bytes=rendered_state,
    )


def _rubrik_export_kind(title: str) -> str | None:
    normalized = " ".join(title.casefold().split())
    if "press release" in normalized or normalized.startswith("news"):
        return "earnings-release"
    return _declared_kind(title, rubrik_quarter=True)


def _wix_export_state_capture(
    exported: _WixVisibleBrowserExport,
    state: _WixVisibleExportState,
) -> WixRenderedPanelCapture:
    if (
        state.requested_year != state.period_end.year
        or state.selected_year != state.requested_year
        or state.quarter_name != _wix_quarter_label(state.period_end)
        or state.heading != _wix_period_heading(state.period_end)
    ):
        raise ApprovedIrObservationCaptureError(
            "Wix visible-browser export selected year or quarter identity is invalid"
        )
    parser = _VisibleAnchorParser()
    parser.feed(state.container_html)
    if state.container_id not in parser.element_ids:
        raise ApprovedIrObservationCaptureError(
            "Wix visible-browser export container identity is not proven by its HTML"
        )
    declared = [(item.url, " ".join(item.title.split())) for item in state.links]
    if len(declared) != len(set(declared)):
        raise ApprovedIrObservationCaptureError(
            "Wix visible-browser export contains duplicate declared links"
        )
    parsed = [
        (url, title)
        for url, title, hidden in parser.anchors
        if not hidden and _declared_kind(title) is not None
    ]
    if sorted(parsed) != sorted(declared):
        raise ApprovedIrObservationCaptureError(
            "Wix visible-browser export links are not exactly proven by container HTML"
        )
    links: list[WixLinkObservation] = []
    for index, (url, title) in enumerate(parsed):
        declared_kind = _declared_kind(title)
        if declared_kind is None:
            raise ApprovedIrObservationCaptureError(
                "Wix visible-browser export contains an unclassified visible link"
            )
        links.append(
            WixLinkObservation(
                title=title,
                url=url,
                declared_kind=declared_kind,
                evidence_locator=(
                    f"visible-quarter[{state.quarter_name}] > anchor[{index}]"
                    f"[text={json.dumps(title, ensure_ascii=True)}]"
                ),
            )
        )
    locator = f"visible-quarter[{state.quarter_name}]"
    rendered_state = _canonical_json(
        {
            "schema_version": _RENDERED_PROOF_SCHEMA_VERSION,
            "page_url": exported.authority_url,
            "document_title": exported.document_title,
            "visible_state": {
                "requested_year": state.requested_year,
                "selected_year": state.selected_year,
                "quarter": state.quarter_name,
                "heading": state.heading,
                "container_id": state.container_id,
                "container_outer_html": state.container_html,
                "links": [item.model_dump(mode="json") for item in links],
            },
        }
    ).encode("utf-8")
    return WixRenderedPanelCapture(
        observation_key=f"wix-{state.period_end.isoformat()}",
        authority_url=exported.authority_url,
        requested_year=state.requested_year,
        selected_year=state.selected_year,
        year_control_locator="external-visible-browser-selected-year",
        requested_quarter_end=state.period_end,
        panels=(
            WixPanelObservation(
                panel_locator=locator,
                quarter_end=state.period_end,
                selected=True,
                visible=True,
                links=tuple(links),
            ),
        ),
        rendered_state_bytes=rendered_state,
        evidence_locator=locator,
    )


def _capture_wix_period(
    page: Any,
    authority_url: str,
    requested_year: int,
    period: date,
) -> WixRenderedPanelCapture:
    quarterly_heading = page.get_by_role("heading", name="Quarterly Results", exact=True)
    if quarterly_heading.count() != 1:
        raise ApprovedIrObservationCaptureError("Wix quarterly-results heading is ambiguous")
    year_locator = quarterly_heading.locator("xpath=following::select[1]")
    if year_locator.count() != 1:
        raise ApprovedIrObservationCaptureError(
            f"Wix year control is ambiguous for {requested_year}"
        )
    control = year_locator.first
    tag_name = str(control.evaluate("element => element.tagName.toLowerCase()"))
    _wait_for_wix_year_control(page, control, requested_year, tag_name)
    current_value = str(control.input_value()).strip() if tag_name == "select" else ""
    current_text = (
        str(control.locator("option:checked").first.inner_text()).strip()
        if tag_name == "select"
        else str(control.inner_text()).strip()
    )
    if tag_name == "select":
        if str(requested_year) not in {current_text, current_value}:
            control.select_option(label=str(requested_year))
    else:
        if str(requested_year) not in str(control.inner_text()).strip():
            control.click()
            options = page.get_by_text(str(requested_year), exact=True)
            visible_options = tuple(
                options.nth(index)
                for index in range(options.count())
                if options.nth(index).is_visible()
            )
            if len(visible_options) != 1:
                raise ApprovedIrObservationCaptureError(
                    f"Wix year option is ambiguous for {requested_year}"
                )
            visible_options[0].click()
    page.wait_for_timeout(500)
    selected_value = str(control.input_value()).strip() if tag_name == "select" else ""
    selected_options = control.locator("option:checked") if tag_name == "select" else control
    selected_text = str(selected_options.first.inner_text()).strip()
    if str(requested_year) not in {selected_text, selected_value}:
        raise ApprovedIrObservationCaptureError("Wix selected year could not be proven")

    quarter_label = _wix_quarter_label(period)
    quarter_controls = page.get_by_role("button", name=quarter_label, exact=True)
    visible_controls = tuple(
        quarter_controls.nth(index)
        for index in range(quarter_controls.count())
        if quarter_controls.nth(index).is_visible()
    )
    if len(visible_controls) != 1:
        raise ApprovedIrObservationCaptureError(
            f"Wix quarter control is ambiguous for {quarter_label}"
        )
    if visible_controls[0].is_enabled():
        visible_controls[0].click()
    page.wait_for_timeout(500)
    period_heading = page.get_by_role("heading", name=_wix_period_heading(period), exact=True)
    if period_heading.count() != 1 or not period_heading.first.is_visible():
        raise ApprovedIrObservationCaptureError("Wix active reporting panel was not proven")
    panel = period_heading.first.locator("xpath=ancestor::div[.//a][1]")
    if panel.count() != 1 or not panel.first.is_visible():
        raise ApprovedIrObservationCaptureError("Wix active reporting panel is ambiguous")
    links = _visible_approved_links(panel.first, authority_url, f"wix-{period.isoformat()}")
    if not links:
        raise ApprovedIrObservationCaptureError(
            f"Wix selected quarter has no visible approved links: {quarter_label}"
        )
    rendered_state = _rendered_state_bytes(
        page,
        {
            "requested_year": requested_year,
            "selected_year": requested_year,
            "quarter": quarter_label,
            "year_control_outer_html": str(control.evaluate("element => element.outerHTML")),
            "quarter_control_outer_html": str(
                visible_controls[0].evaluate("element => element.outerHTML")
            ),
            "panel_outer_html": str(panel.first.evaluate("element => element.outerHTML")),
            "links": [item.model_dump(mode="json") for item in links],
        },
    )
    panel_locator = f"visible-quarter[{quarter_label}]"
    return WixRenderedPanelCapture(
        observation_key=f"wix-{period.isoformat()}",
        authority_url=authority_url,
        requested_year=requested_year,
        selected_year=requested_year,
        year_control_locator="visible year control with exact selected text",
        requested_quarter_end=period,
        panels=(
            WixPanelObservation(
                panel_locator=panel_locator,
                quarter_end=period,
                selected=True,
                visible=True,
                links=links,
            ),
        ),
        rendered_state_bytes=rendered_state,
        evidence_locator=panel_locator,
    )


def _wait_for_wix_year_control(page: Any, control: Any, requested_year: int, tag: str) -> None:
    for _attempt in range(300):
        if tag == "select":
            selected = str(control.locator("option:checked").first.inner_text()).strip()
            choices = tuple(
                str(control.locator("option").nth(index).inner_text()).strip()
                for index in range(control.locator("option").count())
            )
            if str(requested_year) == selected or str(requested_year) in choices:
                return
        elif str(requested_year) in str(control.inner_text()).strip():
            return
        page.wait_for_timeout(100)
    raise ApprovedIrObservationCaptureError("Wix year control did not become ready")


def _capture_rubrik_period(
    page: Any,
    authority_url: str,
    period: date,
) -> RubrikRenderedRowCapture:
    selected_label = _rubrik_tab_label(period)
    tab = page.get_by_role("tab", name=selected_label, exact=True)
    if tab.count() == 1 and tab.first.is_visible():
        if str(tab.first.get_attribute("aria-selected")) != "true":
            tab.first.click()
            page.wait_for_timeout(500)
        container = page.get_by_role("tabpanel", name=selected_label, exact=True)
        if container.count() != 1 or not container.first.is_visible():
            raise ApprovedIrObservationCaptureError("Rubrik reporting tabpanel is ambiguous")
        links = _visible_approved_links(
            container.first,
            authority_url,
            f"rbrk-{period.isoformat()}",
            rubrik_quarter=True,
        )
    else:
        container = page.locator(".module-financial-table table")
        if container.count() != 1:
            raise ApprovedIrObservationCaptureError(
                f"Rubrik reporting evidence was not found for {period.isoformat()}"
            )
        links = _rubrik_financial_table_links(
            container.first, selected_label, f"rbrk-{period.isoformat()}"
        )
    if not links:
        raise ApprovedIrObservationCaptureError(
            "Rubrik reporting row has no visible approved links"
        )
    row_locator = f"visible-row[{selected_label}]"
    rendered_state = _rendered_state_bytes(
        page,
        {
            "row": selected_label,
            "outer_html": str(container.first.evaluate("element => element.outerHTML")),
            "links": [item.model_dump(mode="json") for item in links],
        },
    )
    return RubrikRenderedRowCapture(
        observation_key=f"rbrk-{period.isoformat()}",
        authority_url=authority_url,
        quarter_end=period,
        row_locator=row_locator,
        links=tuple(RubrikLinkObservation.model_validate(item.model_dump()) for item in links),
        rendered_state_bytes=rendered_state,
    )


def _rubrik_financial_table_links(
    table: Any,
    period_label: str,
    observation_key: str,
) -> tuple[WixLinkObservation, ...]:
    requested = (
        ("Press Release", "earnings-release"),
        ("Earnings Presentation", "presentation"),
    )
    links: list[WixLinkObservation] = []
    for suffix, kind in requested:
        label = f"{period_label} {suffix}"
        anchor = table.locator(f'a[aria-label="{label}"]:visible')
        if anchor.count() != 1:
            raise ApprovedIrObservationCaptureError(
                f"Rubrik financial table link is missing or ambiguous: {label}"
            )
        links.append(
            WixLinkObservation(
                title=suffix,
                url=str(anchor.first.get_attribute("href") or ""),
                declared_kind=kind,
                evidence_locator=f'{observation_key} > visible-anchor[aria-label="{label}"]',
            )
        )
    return tuple(links)


def _visible_approved_links(
    scope: Any,
    authority_url: str,
    observation_key: str,
    rubrik_quarter: bool = False,
) -> tuple[WixLinkObservation, ...]:
    anchors = scope.locator("a[href]:visible")
    links: list[WixLinkObservation] = []
    for index in range(anchors.count()):
        anchor = anchors.nth(index)
        title = " ".join(str(anchor.inner_text()).split())
        kind = _declared_kind(title, rubrik_quarter=rubrik_quarter)
        if kind is None:
            continue
        href = str(anchor.get_attribute("href") or "").strip()
        if not href:
            continue
        links.append(
            WixLinkObservation(
                title=title,
                url=href,
                declared_kind=kind,
                evidence_locator=(
                    f"{observation_key} > visible-anchor[{index}]"
                    f"[text={json.dumps(title, ensure_ascii=True)}]"
                ),
            )
        )
    if len({item.url for item in links}) != len(links):
        raise ApprovedIrObservationCaptureError(
            f"visible reporting state contains duplicate candidate URLs at {authority_url}"
        )
    return tuple(links)


def _declared_kind(title: str, *, rubrik_quarter: bool = False) -> str | None:
    normalized = " ".join(title.casefold().split())
    if "webcast" in normalized or "listen" in normalized or "replay" in normalized:
        return "webcast"
    if "10-q" in normalized or "10-k" in normalized or "20-f" in normalized or "6-k" in normalized:
        return "sec-filing"
    if "transcript" in normalized or "prepared remarks" in normalized:
        return "transcript"
    if "shareholder" in normalized and "update" in normalized:
        return "investor-update"
    if "presentation" in normalized or "slides" in normalized:
        return "presentation"
    if rubrik_quarter and normalized.startswith("news"):
        return "earnings-release"
    if "earnings" in normalized and (
        "release" in normalized or "results" in normalized or "announcement" in normalized
    ):
        return "earnings-release"
    if normalized in {"press release", "financial results"}:
        return "earnings-release"
    return None


def _wix_quarter_label(period: date) -> str:
    return f"{((period.month - 1) // 3) + 1}Q {period.year}"


def _wix_period_heading(period: date) -> str:
    quarter = ("First", "Second", "Third", "Fourth")[(period.month - 1) // 3]
    return f"{quarter} Quarter {period.year}"


def _rubrik_tab_label(period: date) -> str:
    fiscal_year = period.year + 1 if period.month in {4, 7, 10} else period.year
    quarter = {4: 1, 7: 2, 10: 3, 1: 4}[period.month]
    return f"Q{quarter} {fiscal_year}"


def _rendered_state_bytes(page: Any, state: dict[str, object]) -> bytes:
    return _canonical_json(
        {
            "schema_version": _RENDERED_PROOF_SCHEMA_VERSION,
            "page_url": str(page.url),
            "document_title": str(page.title()),
            "visible_state": state,
        }
    ).encode("utf-8")


class SealedObservationArtifact(_FrozenModel):
    artifact_id: str
    observation_key: str
    role: ObservationArtifactRole
    content_bytes: bytes
    sha256: str
    byte_size: int = Field(ge=0)
    media_type: str = Field(min_length=1, max_length=255)
    source_url: str
    evidence_locator: str
    observed_at: datetime
    retrieved_at: datetime

    @model_validator(mode="after")
    def _verify_identity(self) -> Self:
        digest = hashlib.sha256(self.content_bytes).hexdigest()
        if digest != self.sha256 or len(self.content_bytes) != self.byte_size:
            raise ValueError("artifact digest or byte size does not match exact content")
        expected_id = _artifact_id(
            self.observation_key,
            self.role,
            digest,
            self.source_url,
            self.evidence_locator,
        )
        if self.artifact_id != expected_id:
            raise ValueError("artifact identity does not match canonical metadata")
        if self.retrieved_at < self.observed_at:
            raise ValueError("artifact retrieval cannot precede observation")
        return self


class _ArtifactWire(_FrozenModel):
    artifact_id: str
    observation_key: str
    role: ObservationArtifactRole
    content_bytes: str
    sha256: str
    byte_size: int
    media_type: str
    source_url: str
    evidence_locator: str
    observed_at: datetime
    retrieved_at: datetime


class _BundleWire(_FrozenModel):
    schema_version: str
    issuer_identifier: str
    authority_url: str
    normalized_observations_bytes: str
    artifacts: tuple[_ArtifactWire, ...]
    captured_at: datetime
    bundle_sha256: str


class ApprovedIrObservationBundle(_FrozenModel):
    schema_version: str
    issuer_identifier: str
    authority_url: str
    normalized_observations_bytes: bytes
    artifacts: tuple[SealedObservationArtifact, ...]
    captured_at: datetime
    bundle_sha256: str

    def to_bytes(self) -> bytes:
        return _canonical_json(_bundle_wire(self)).encode("utf-8")

    @model_validator(mode="after")
    def _verify_bundle(self) -> Self:
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError("unsupported approved IR observation bundle schema")
        expected = hashlib.sha256(
            _canonical_json(_bundle_unsigned_wire(self)).encode("utf-8")
        ).hexdigest()
        if self.bundle_sha256 != expected:
            raise ValueError("bundle hash does not match canonical sealed payload")
        if len({item.artifact_id for item in self.artifacts}) != len(self.artifacts):
            raise ValueError("bundle artifact identities must be unique")
        return self


def collect_approved_ir_observations(
    request: ApprovedIrObservationCaptureRequest,
    *,
    browser: ApprovedIrBrowser,
    robots_allows: Callable[[str, str], bool],
) -> ApprovedIrObservationBundle:
    """Capture public authority bytes plus exact visible rendered states."""

    request = ApprovedIrObservationCaptureRequest.model_validate(request.model_dump())
    policy = issuer_policy(request.issuer_identifier)
    authority_url = policy.ir.authority_url
    if not robots_allows(authority_url, request.user_agent):
        raise ApprovedIrObservationCaptureError("publisher authority is denied by robots policy")
    authority = browser.fetch_authority(
        authority_url,
        user_agent=request.user_agent,
        timeout_ms=request.timeout_ms,
    )
    if authority.status_code in {401, 403}:
        raise ApprovedIrObservationAuthenticationError(
            "publisher authority requires authentication or authorization"
        )
    if authority.status_code != 200:
        raise ApprovedIrObservationCaptureError(
            f"publisher authority returned HTTP {authority.status_code}"
        )
    if authority.requested_url != authority_url or authority.final_url != authority_url:
        raise ApprovedIrObservationCaptureError("publisher authority redirect is not admissible")

    artifacts = [
        _seal_observation_artifact(
            observation_key="publisher-authority",
            role=ObservationArtifactRole.AUTHORITY_RAW,
            content_bytes=authority.content_bytes,
            media_type=authority.media_type,
            source_url=authority_url,
            evidence_locator="publisher-authority-response",
            observed_at=request.captured_at,
            retrieved_at=request.captured_at,
        )
    ]
    try:
        if policy.ir.adapter_key is AdapterKey.WIX_VISIBLE_QUARTER:
            normalized, rendered = _collect_wix(request, authority_url, browser)
        elif policy.ir.adapter_key is AdapterKey.RUBRIK_QUARTER_TABLE:
            normalized, rendered = _collect_rubrik(request, authority_url, browser)
        else:  # pragma: no cover - reviewed registry currently exposes only these adapters
            raise ApprovedIrObservationCaptureError("issuer has no approved observation collector")
    except IrCatalogError as exc:
        raise ApprovedIrObservationCaptureError(str(exc)) from None
    artifacts.extend(rendered)
    return _seal_approved_ir_observation_bundle(
        issuer_identifier=policy.issuer_id,
        authority_url=authority_url,
        normalized_observations_bytes=normalized,
        artifacts=tuple(artifacts),
        captured_at=request.captured_at,
    )


def public_robots_allows(url: str, user_agent: str) -> bool:
    """Fail-closed robots check through the repository public-only transport."""

    parsed = urllib.parse.urlparse(url)
    robots_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/robots.txt", "", "", ""))
    try:
        request = urllib.request.Request(
            robots_url,
            headers={"User-Agent": user_agent, "Accept": "text/plain, */*;q=0.1"},
        )
        with build_public_opener().open(request, timeout=15) as response:
            content = response.read(1_000_001)
        if len(content) > 1_000_000:
            return False
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(content.decode("utf-8", errors="replace").splitlines())
        return parser.can_fetch(user_agent, url)
    except (OSError, UnicodeError, ValueError, urllib.error.URLError):
        return False


def _seal_observation_artifact(
    *,
    observation_key: str,
    role: ObservationArtifactRole,
    content_bytes: bytes,
    media_type: str,
    source_url: str,
    evidence_locator: str,
    observed_at: datetime,
    retrieved_at: datetime,
) -> SealedObservationArtifact:
    digest = hashlib.sha256(content_bytes).hexdigest()
    return SealedObservationArtifact(
        artifact_id=_artifact_id(observation_key, role, digest, source_url, evidence_locator),
        observation_key=observation_key,
        role=role,
        content_bytes=content_bytes,
        sha256=digest,
        byte_size=len(content_bytes),
        media_type=media_type,
        source_url=source_url,
        evidence_locator=evidence_locator,
        observed_at=observed_at,
        retrieved_at=retrieved_at,
    )


def _seal_approved_ir_observation_bundle(
    *,
    issuer_identifier: str,
    authority_url: str,
    normalized_observations_bytes: bytes,
    artifacts: tuple[SealedObservationArtifact, ...],
    captured_at: datetime,
) -> ApprovedIrObservationBundle:
    unsigned = {
        "schema_version": _SCHEMA_VERSION,
        "issuer_identifier": issuer_identifier,
        "authority_url": authority_url,
        "normalized_observations_bytes": _b64(normalized_observations_bytes),
        "artifacts": [_artifact_wire(item) for item in artifacts],
        "captured_at": captured_at.isoformat(),
    }
    digest = hashlib.sha256(_canonical_json(unsigned).encode("utf-8")).hexdigest()
    bundle = ApprovedIrObservationBundle(
        schema_version=_SCHEMA_VERSION,
        issuer_identifier=issuer_identifier,
        authority_url=authority_url,
        normalized_observations_bytes=normalized_observations_bytes,
        artifacts=artifacts,
        captured_at=captured_at,
        bundle_sha256=digest,
    )
    _validate_bundle_semantics(bundle)
    return bundle


def load_approved_ir_observation_bundle(wire_bytes: bytes) -> ApprovedIrObservationBundle:
    if not wire_bytes or len(wire_bytes) > _MAX_WIRE_BYTES:
        raise ApprovedIrObservationCaptureError("approved IR bundle size is invalid")
    try:
        wire = _BundleWire.model_validate_json(wire_bytes)
        artifacts = tuple(_artifact_from_wire(item) for item in wire.artifacts)
        bundle = ApprovedIrObservationBundle(
            schema_version=wire.schema_version,
            issuer_identifier=wire.issuer_identifier,
            authority_url=wire.authority_url,
            normalized_observations_bytes=_decode_b64(wire.normalized_observations_bytes),
            artifacts=artifacts,
            captured_at=wire.captured_at,
            bundle_sha256=wire.bundle_sha256,
        )
    except (KeyError, TypeError, ValueError, UnicodeError, ValidationError) as exc:
        raise ApprovedIrObservationCaptureError(
            f"approved IR bundle digest/hash/byte size validation failed: {exc}"
        ) from None
    if wire_bytes != bundle.to_bytes():
        raise ApprovedIrObservationCaptureError("approved IR bundle wire is not canonical")
    _validate_bundle_semantics(bundle)
    return bundle


def _collect_wix(
    request: ApprovedIrObservationCaptureRequest,
    authority_url: str,
    browser: ApprovedIrBrowser,
) -> tuple[bytes, list[SealedObservationArtifact]]:
    by_year: dict[int, list[date]] = defaultdict(list)
    for period in request.requested_quarter_ends:
        by_year[period.year].append(period)
    captures: list[WixRenderedPanelCapture] = []
    for year, periods in by_year.items():
        captures.extend(
            browser.capture_wix_year(
                authority_url,
                requested_year=year,
                requested_quarter_ends=tuple(periods),
                user_agent=request.user_agent,
                timeout_ms=request.timeout_ms,
            )
        )
    if len(captures) != len(request.requested_quarter_ends):
        raise ApprovedIrObservationCaptureError("Wix collector returned an incomplete period set")
    observations: list[WixRenderedObservation] = []
    artifacts: list[SealedObservationArtifact] = []
    for capture in captures:
        if (
            capture.authority_url != authority_url
            or capture.requested_year != capture.requested_quarter_end.year
            or capture.selected_year != capture.requested_year
        ):
            raise ApprovedIrObservationCaptureError(
                "Wix selected year does not match the requested reporting period"
            )
        if any(panel.links for panel in capture.panels if not panel.visible):
            raise ApprovedIrObservationCaptureError("hidden Wix panel exposed candidate anchors")
        digest = hashlib.sha256(capture.rendered_state_bytes).hexdigest()
        observations.append(
            WixRenderedObservation(
                observation_key=capture.observation_key,
                authority_url=authority_url,
                raw_sha256=digest,
                requested_year=capture.requested_year,
                selected_year=capture.selected_year,
                year_control_locator=capture.year_control_locator,
                requested_quarter_end=capture.requested_quarter_end,
                panels=capture.panels,
            )
        )
        artifacts.append(
            _seal_observation_artifact(
                observation_key=capture.observation_key,
                role=ObservationArtifactRole.RENDERED_STATE,
                content_bytes=capture.rendered_state_bytes,
                media_type="application/json",
                source_url=authority_url,
                evidence_locator=capture.evidence_locator,
                observed_at=request.captured_at,
                retrieved_at=request.captured_at,
            )
        )
    ordered = sorted(
        observations,
        key=lambda item: request.requested_quarter_ends.index(item.requested_quarter_end),
    )
    normalized = _canonical_json([item.model_dump(mode="json") for item in ordered]).encode("utf-8")
    policy = issuer_policy(request.issuer_identifier)
    parsed = parse_wix_visible_quarters(
        load_wix_rendered_observations(normalized.decode()), policy=policy
    )
    build_catalog(policy, parsed)
    return normalized, artifacts


def _collect_rubrik(
    request: ApprovedIrObservationCaptureRequest,
    authority_url: str,
    browser: ApprovedIrBrowser,
) -> tuple[bytes, list[SealedObservationArtifact]]:
    captures = browser.capture_rubrik_rows(
        authority_url,
        requested_quarter_ends=request.requested_quarter_ends,
        user_agent=request.user_agent,
        timeout_ms=request.timeout_ms,
    )
    if len(captures) != len(request.requested_quarter_ends):
        raise ApprovedIrObservationCaptureError("Rubrik collector returned an incomplete row set")
    observations: list[RubrikQuarterObservation] = []
    artifacts: list[SealedObservationArtifact] = []
    for capture in captures:
        if capture.authority_url != authority_url:
            raise ApprovedIrObservationCaptureError("Rubrik row authority changed")
        digest = hashlib.sha256(capture.rendered_state_bytes).hexdigest()
        observations.append(
            RubrikQuarterObservation(
                observation_key=capture.observation_key,
                authority_url=authority_url,
                raw_sha256=digest,
                quarter_end=capture.quarter_end,
                row_locator=capture.row_locator,
                links=capture.links,
            )
        )
        artifacts.append(
            _seal_observation_artifact(
                observation_key=capture.observation_key,
                role=ObservationArtifactRole.RENDERED_STATE,
                content_bytes=capture.rendered_state_bytes,
                media_type="application/json",
                source_url=authority_url,
                evidence_locator=capture.row_locator,
                observed_at=request.captured_at,
                retrieved_at=request.captured_at,
            )
        )
    ordered = sorted(
        observations,
        key=lambda item: request.requested_quarter_ends.index(item.quarter_end),
    )
    normalized = _canonical_json([item.model_dump(mode="json") for item in ordered]).encode("utf-8")
    policy = issuer_policy(request.issuer_identifier)
    parsed = parse_rubrik_quarter_rows(
        load_rubrik_row_observations(normalized.decode()), policy=policy
    )
    build_catalog(policy, parsed)
    return normalized, artifacts


def _validate_bundle_semantics(bundle: ApprovedIrObservationBundle) -> None:
    policy = issuer_policy(bundle.issuer_identifier)
    if bundle.authority_url != policy.ir.authority_url:
        raise ApprovedIrObservationCaptureError("bundle authority does not match issuer policy")
    authorities = [
        item for item in bundle.artifacts if item.role is ObservationArtifactRole.AUTHORITY_RAW
    ]
    rendered = {
        item.observation_key: item
        for item in bundle.artifacts
        if item.role is ObservationArtifactRole.RENDERED_STATE
    }
    if len(authorities) != 1:
        raise ApprovedIrObservationCaptureError("bundle requires exactly one authority artifact")
    try:
        text = bundle.normalized_observations_bytes.decode("utf-8")
        if policy.ir.adapter_key is AdapterKey.WIX_VISIBLE_QUARTER:
            observations = load_wix_rendered_observations(text)
        else:
            observations = load_rubrik_row_observations(text)
    except (UnicodeError, ValidationError) as exc:
        raise ApprovedIrObservationCaptureError(
            f"normalized observation payload is invalid: {exc}"
        ) from None
    if set(rendered) != {item.observation_key for item in observations}:
        raise ApprovedIrObservationCaptureError(
            "bundle requires exactly one rendered state per normalized observation"
        )
    for observation in observations:
        artifact = rendered[observation.observation_key]
        if observation.raw_sha256 != artifact.sha256:
            raise ApprovedIrObservationCaptureError(
                "normalized raw_sha256 does not match rendered artifact bytes"
            )
        _validate_rendered_link_proof(observation, artifact)


def _validate_rendered_link_proof(
    observation: WixRenderedObservation | RubrikQuarterObservation,
    artifact: SealedObservationArtifact,
) -> None:
    try:
        proof_value = json.loads(artifact.content_bytes)
    except (UnicodeError, json.JSONDecodeError):
        raise ApprovedIrObservationCaptureError(
            "rendered evidence is not canonical JSON link proof"
        ) from None
    if not isinstance(proof_value, dict):
        raise ApprovedIrObservationCaptureError(
            "rendered evidence is not canonical JSON link proof"
        )
    proof = cast("dict[str, object]", proof_value)
    if _canonical_json(proof).encode() != artifact.content_bytes:
        raise ApprovedIrObservationCaptureError(
            "rendered evidence is not canonical JSON link proof"
        )
    state_value = proof.get("visible_state")
    if (
        proof.get("schema_version") != _RENDERED_PROOF_SCHEMA_VERSION
        or proof.get("page_url") != observation.authority_url
        or not isinstance(state_value, dict)
    ):
        raise ApprovedIrObservationCaptureError("rendered evidence identity is invalid")
    state = cast("dict[str, object]", state_value)
    if isinstance(observation, WixRenderedObservation):
        selected = tuple(panel for panel in observation.panels if panel.selected and panel.visible)
        if len(selected) != 1:
            raise ApprovedIrObservationCaptureError("Wix rendered proof has no selected panel")
        links = selected[0].links
        expected_locator = selected[0].panel_locator
    else:
        links = observation.links
        expected_locator = observation.row_locator
    expected_links = [item.model_dump(mode="json") for item in links]
    if state.get("links") != expected_links or artifact.evidence_locator != expected_locator:
        raise ApprovedIrObservationCaptureError(
            "normalized links are not proven by exact rendered evidence bytes"
        )


def _artifact_id(
    observation_key: str,
    role: ObservationArtifactRole,
    digest: str,
    source_url: str,
    evidence_locator: str,
) -> str:
    payload = {
        "observation_key": observation_key,
        "role": role.value,
        "sha256": digest,
        "source_url": source_url,
        "evidence_locator": evidence_locator,
    }
    return (
        "ir-observation-artifact:"
        + hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    )


def _artifact_wire(item: SealedObservationArtifact) -> dict[str, object]:
    return {
        "artifact_id": item.artifact_id,
        "observation_key": item.observation_key,
        "role": item.role.value,
        "content_bytes": _b64(item.content_bytes),
        "sha256": item.sha256,
        "byte_size": item.byte_size,
        "media_type": item.media_type,
        "source_url": item.source_url,
        "evidence_locator": item.evidence_locator,
        "observed_at": item.observed_at.isoformat(),
        "retrieved_at": item.retrieved_at.isoformat(),
    }


def _artifact_from_wire(value: _ArtifactWire) -> SealedObservationArtifact:
    return SealedObservationArtifact(
        artifact_id=value.artifact_id,
        observation_key=value.observation_key,
        role=value.role,
        content_bytes=_decode_b64(value.content_bytes),
        sha256=value.sha256,
        byte_size=value.byte_size,
        media_type=value.media_type,
        source_url=value.source_url,
        evidence_locator=value.evidence_locator,
        observed_at=value.observed_at,
        retrieved_at=value.retrieved_at,
    )


def _bundle_unsigned_wire(bundle: ApprovedIrObservationBundle) -> dict[str, object]:
    return {
        "schema_version": bundle.schema_version,
        "issuer_identifier": bundle.issuer_identifier,
        "authority_url": bundle.authority_url,
        "normalized_observations_bytes": _b64(bundle.normalized_observations_bytes),
        "artifacts": [_artifact_wire(item) for item in bundle.artifacts],
        "captured_at": bundle.captured_at.isoformat(),
    }


def _bundle_wire(bundle: ApprovedIrObservationBundle) -> dict[str, object]:
    return {**_bundle_unsigned_wire(bundle), "bundle_sha256": bundle.bundle_sha256}


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode_b64(value: object) -> bytes:
    if not isinstance(value, str):
        raise ValueError("base64 bytes must be a string")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("invalid canonical base64 bytes") from None


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


__all__ = [
    "ApprovedIrBrowser",
    "ApprovedIrObservationAuthenticationError",
    "ApprovedIrObservationBundle",
    "ApprovedIrObservationCaptureError",
    "ApprovedIrObservationCaptureRequest",
    "ObservationArtifactRole",
    "PlaywrightApprovedIrBrowser",
    "PublisherAuthorityResponse",
    "RubrikRenderedRowCapture",
    "SealedObservationArtifact",
    "WixRenderedPanelCapture",
    "collect_approved_ir_observations",
    "import_rubrik_visible_browser_export",
    "import_wix_visible_browser_export",
    "load_approved_ir_observation_bundle",
    "public_robots_allows",
]
