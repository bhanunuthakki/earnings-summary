"""Create a separate offline reading copy of a recognized workspace report.

This removes application access and hidden comments. Visible analysis remains
visible and requires the owner's review before sharing.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
from html.parser import HTMLParser
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Comment

from log_redact import redact
from report.renderers.workspace_script import JS

MAX_INPUT_BYTES = 32 * 1024 * 1024


class ShareableReportError(ValueError):
    """Input cannot be safely exported as a supported reading copy."""


class _BalancedHTML(HTMLParser):
    """Reject damaged markup instead of relying on parser repair."""

    _VOID = frozenset(
        [
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        ]
    )

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names = [name for name, _value in attrs]
        if len(names) != len(set(names)):
            raise ShareableReportError("duplicate HTML attributes")
        if tag not in self._VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack.pop() != tag:
            raise ShareableReportError("unbalanced HTML")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in self._VOID:
            self.handle_endtag(tag)


def _json_payload(soup: BeautifulSoup, identity: str) -> dict[str, object]:
    matches = soup.select(f'[id="{identity}"]')
    if len(matches) != 1:
        raise ShareableReportError("missing or duplicate workspace payload")
    node = matches[0]
    if node.name != "script" or node.get("type") != "application/json":
        raise ShareableReportError("unsupported workspace payload")
    try:
        value: object = json.loads(node.string or "")
    except ValueError:
        raise ShareableReportError("invalid workspace JSON") from None
    if not isinstance(value, dict):
        raise ShareableReportError("unsupported workspace JSON")
    return cast(dict[str, object], value)


def _public_link(value: str) -> bool:
    if value.startswith("#"):
        return True
    try:
        url = urlsplit(value)
        hostname = (url.hostname or "").lower().rstrip(".")
        if url.scheme not in {"https", "http"} or url.username or url.password:
            return False
        if not hostname or redact(value) != value:
            return False
        try:
            return ipaddress.ip_address(hostname).is_global
        except ValueError:
            # Browsers also accept shortened, octal and hexadecimal IPv4 forms.
            last_label = hostname.rsplit(".", 1)[-1]
            return (
                not (last_label.isdigit() or last_label.startswith("0x"))
                and "." in hostname
                and not hostname.endswith(
                    (".localhost", ".local", ".internal", ".lan", ".ts.net", ".home.arpa")
                )
            )
    except ValueError:
        return False


def _reading_copy(text: str) -> str:
    # HTML parsing normalizes source line endings; keep the input file untouched.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    parser = _BalancedHTML()
    parser.feed(text)
    parser.close()
    if parser.stack:
        raise ShareableReportError("incomplete HTML")
    soup = BeautifulSoup(text, "html.parser")
    for selector in ("html", "head", "body", ".l1-root"):
        if len(soup.select(selector)) != 1:
            raise ShareableReportError("unsupported workspace structure")
    if soup.head is None or soup.body is None:
        raise ShareableReportError("unsupported workspace structure")
    if soup.head.parent != soup.html or soup.body.parent != soup.html:
        raise ShareableReportError("unsupported workspace structure")
    boot = _json_payload(soup, "workspace-boot")
    comments = _json_payload(soup, "workspace-comments")
    capability = boot.get("report_capability")
    if not isinstance(capability, str) or not isinstance(comments.get("comments"), list):
        raise ShareableReportError("unsupported workspace payload shape")
    if sum(node.string == JS for node in soup.select("script")) != 1:
        raise ShareableReportError("unsupported navigation script")

    for node in soup.find_all(string=lambda value: isinstance(value, Comment)):
        node.extract()
    for node in soup.select(
        "script, link, base, iframe, frame, frameset, object, embed, portal, noscript, "
        "form, input, textarea, select, audio, video, source, track, "
        "#cmt-sidebar, #chat-sidebar, .cmt-sidebar, .chat-sidebar"
    ):
        node.decompose()
    for node in soup.select("meta"):
        if node.get("http-equiv"):
            node.decompose()

    reading_data = {
        "data-theme",
        "data-density",
        "data-tab",
        "data-subtab",
        "data-tab-group",
        "data-quarter",
        "data-quarter-group",
        "data-quarter-card",
        "data-xtab",
        "data-anchor",
        "data-drill-target",
        "data-persist",
        "data-comp-member-search",
    }
    for node in soup.find_all(True):
        for name in list(node.attrs):
            if (
                name.startswith("on")
                or (name.startswith("data-") and name not in reading_data)
                or name
                in {
                    "src",
                    "srcset",
                    "srcdoc",
                    "poster",
                    "background",
                    "action",
                    "formaction",
                    "ping",
                    "autofocus",
                    "contenteditable",
                    "xlink:href",
                }
            ):
                value = node.attrs.pop(name)
                if (
                    node.name == "img"
                    and name == "src"
                    and isinstance(value, str)
                    and value.lower().startswith(
                        (
                            "data:image/png;",
                            "data:image/jpeg;",
                            "data:image/gif;",
                            "data:image/webp;",
                        )
                    )
                ):
                    node["src"] = value
        if node.has_attr("href"):
            href = node.get("href")
            if node.name != "a" or not isinstance(href, str) or not _public_link(href):
                del node["href"]
            else:
                node["rel"] = "noopener noreferrer"
                node["referrerpolicy"] = "no-referrer"
        if node.name == "button" and not (
            node.has_attr("data-tab")
            or node.has_attr("data-subtab")
            or node.has_attr("data-quarter")
            or node.get("id") == "report-sidebar-toggle"
        ):
            node["disabled"] = ""
            node["aria-disabled"] = "true"

    banner = soup.new_tag("p", attrs={"class": "k-well k-well-warn"})
    banner.string = (
        "Read-only copy. Application access and hidden comments were removed. "
        "Visible analysis can contain private information. Owner review is required before sharing."
    )
    root = soup.select_one(".l1-root")
    assert root is not None
    root.insert(0, banner)
    digest = base64.b64encode(hashlib.sha256(JS.encode("utf-8")).digest()).decode("ascii")
    policy = soup.new_tag("meta")
    policy["http-equiv"] = "Content-Security-Policy"
    policy["content"] = (
        f"default-src 'none'; script-src 'sha256-{digest}'; style-src 'unsafe-inline'; "
        "img-src data:; connect-src 'none'; form-action 'none'; base-uri 'none'; "
        "object-src 'none'; frame-src 'none'"
    )
    soup.head.insert(0, policy)
    referrer = soup.new_tag("meta", attrs={"name": "referrer", "content": "no-referrer"})
    soup.head.insert(1, referrer)
    script = soup.new_tag("script")
    script.string = JS
    soup.body.append(script)
    result = str(soup)
    if capability and capability in result:
        raise ShareableReportError("application capability remains in visible content")
    if soup.select("#workspace-boot, #workspace-comments"):
        raise ShareableReportError("private payload remains")
    return result


def export_shareable_report(source: Path, destination: Path) -> Path:
    """Export a new file without modifying the source or an existing file."""
    if source.resolve() == destination.resolve():
        raise ShareableReportError("destination must be a new, separate file")
    with source.open("rb") as stream:
        raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ShareableReportError("report exceeds the 32 MiB input limit")
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        raise ShareableReportError("report must be UTF-8") from None
    output = _reading_copy(text).encode("utf-8")
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    owned = os.fstat(descriptor)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(output)
    except BaseException:
        try:
            current = destination.lstat()
            if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
                destination.unlink()
        except FileNotFoundError:
            pass
        raise
    return destination
