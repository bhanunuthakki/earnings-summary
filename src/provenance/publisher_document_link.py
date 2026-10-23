"""Verify a publisher page's explicit link to a document.

The Nordic context is parsed as JSON data. This module never evaluates its
JavaScript wrapper or any trailing script content.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal, cast
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

from runtime.deterministic import content_sha256_ascii


class PublisherDocumentLink(BaseModel):
    """A frozen proof of one explicit publisher-page document link."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    publisher_page_observation_id: str = Field(min_length=1, max_length=128)
    publisher_page_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    format: Literal["nordic_rendering_json/v1"] = "nordic_rendering_json/v1"
    json_path: tuple[StrictStr | StrictInt, ...] = Field(min_length=1, max_length=32)
    link_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("publisher_page_observation_id")
    @classmethod
    def _observation_id_nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("publisher_document_link_observation_id_invalid")
        return value

    @field_validator("json_path")
    @classmethod
    def _path_components_valid(
        cls, value: tuple[StrictStr | StrictInt, ...]
    ) -> tuple[StrictStr | StrictInt, ...]:
        if not value:
            raise ValueError("publisher_document_link_path_empty")
        for component in value:
            if isinstance(component, str) and not component:
                raise ValueError("publisher_document_link_path_key_empty")
            if isinstance(component, int) and component < 0:
                raise ValueError("publisher_document_link_path_index_negative")
        return value


def verify_publisher_document_link(
    raw: bytes, proof: PublisherDocumentLink, expected_url: str
) -> None:
    """Verify a retained page and one selected JSON link, or raise a safe code."""
    if hashlib.sha256(raw).hexdigest() != proof.publisher_page_sha256:
        raise ValueError("publisher_document_link_page_sha256_mismatch")

    try:
        target = urlsplit(expected_url)
        valid_url = (
            target.scheme == "https"
            and bool(target.hostname)
            and target.username is None
            and target.password is None
        )
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ValueError("publisher_document_link_expected_url_invalid")

    soup = BeautifulSoup(raw, "html.parser")
    scripts = soup.find_all("script", id="__NORDIC_RENDERING_CTX__")
    if len(scripts) != 1:
        raise ValueError("publisher_document_link_script_missing_or_duplicate")
    script_text = scripts[0].get_text()
    prefix = "_n.ctx.r="
    stripped = script_text.strip()
    if not stripped.startswith(prefix):
        raise ValueError("publisher_document_link_context_prefix_invalid")
    json_text = stripped[len(prefix) :].lstrip()
    try:
        selected: object = json.JSONDecoder().raw_decode(json_text)[0]
    except (json.JSONDecodeError, RecursionError):
        raise ValueError("publisher_document_link_context_json_invalid") from None

    try:
        for component in proof.json_path:
            if isinstance(component, str):
                if not isinstance(selected, dict):
                    raise KeyError
                selected_dict = cast(dict[str, object], selected)
                if component not in selected_dict:
                    raise KeyError
                selected = selected_dict[component]
            else:
                if not isinstance(selected, list):
                    raise IndexError
                selected_list = cast(list[object], selected)
                if component >= len(selected_list):
                    raise IndexError
                selected = selected_list[component]
    except (KeyError, IndexError, TypeError):
        raise ValueError("publisher_document_link_path_invalid") from None

    if not isinstance(selected, dict):
        raise ValueError("publisher_document_link_target_invalid")
    selected_dict = cast(dict[str, object], selected)
    href = selected_dict.get("href")
    if href != expected_url:
        raise ValueError("publisher_document_link_href_mismatch")
    label = selected_dict.get("label")
    if not isinstance(label, str) or not label.strip():
        raise ValueError("publisher_document_link_label_invalid")
    if content_sha256_ascii(selected_dict) != proof.link_sha256:
        raise ValueError("publisher_document_link_sha256_mismatch")
