from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from execution import filing_xbrl_bridge as bridge
from filings.xbrl_units import canonicalize_xbrl_unit

ISO = "http://www.xbrl.org/2003/iso4217"
XBRLI = "http://www.xbrl.org/2003/instance"


def test_real_inline_fixture_unit_is_measure_based_not_source_id() -> None:
    fixture = Path(__file__).parent / "fixtures" / "filing_xbrl_bridge_smoke.xhtml"
    tree = ET.parse(fixture)
    measure = tree.find(f".//{{{XBRLI}}}unit/{{{XBRLI}}}measure")
    assert measure is not None
    assert measure.text == "iso4217:USD"
    assert canonicalize_xbrl_unit(((ISO, "USD"),), ()) == ("USD", "USD")
    fixture_qname = SimpleNamespace(namespaceURI=ISO, localName=measure.text.split(":")[1])
    fact = SimpleNamespace(unit=SimpleNamespace(measures=((fixture_qname,), ())))
    fact_unit = cast(Callable[..., tuple[str, str | None]], getattr(bridge, "_fact_unit"))
    assert fact_unit(fact, "u17") == ("USD", "USD")


def test_bridge_retains_division_from_xbrl_unit_xml() -> None:
    unit = ET.fromstring(f"""
        <xbrli:unit xmlns:xbrli="{XBRLI}" id="uEPS">
          <xbrli:divide>
            <xbrli:unitNumerator><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unitNumerator>
            <xbrli:unitDenominator><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unitDenominator>
          </xbrli:divide>
        </xbrli:unit>
    """)
    numerator = unit.find(f".//{{{XBRLI}}}unitNumerator/{{{XBRLI}}}measure")
    denominator = unit.find(f".//{{{XBRLI}}}unitDenominator/{{{XBRLI}}}measure")
    assert numerator is not None and numerator.text == "iso4217:USD"
    assert denominator is not None and denominator.text == "xbrli:shares"
    measures = (
        (SimpleNamespace(namespaceURI=ISO, localName=numerator.text.split(":")[1]),),
        (SimpleNamespace(namespaceURI=XBRLI, localName=denominator.text.split(":")[1]),),
    )
    fact = SimpleNamespace(unit=SimpleNamespace(measures=measures))
    fact_unit = cast(Callable[..., tuple[str, str | None]], getattr(bridge, "_fact_unit"))
    assert fact_unit(fact, unit.attrib["id"]) == ("USD/shares", "USD")


def test_divided_units_retain_denominator_multiplicity_and_order_independence() -> None:
    assert canonicalize_xbrl_unit(((ISO, "USD"),), ((XBRLI, "shares"),)) == ("USD/shares", "USD")
    numerator = ((ISO, "USD"), (XBRLI, "pure"), (ISO, "USD"))
    denominator = ((XBRLI, "shares"), (XBRLI, "shares"))
    first = canonicalize_xbrl_unit(numerator, denominator)
    assert first == ("(USD*USD*pure)/(shares*shares)", None)
    assert first == canonicalize_xbrl_unit(tuple(reversed(numerator)), denominator)


def test_unknown_namespace_does_not_impersonate_currency() -> None:
    assert canonicalize_xbrl_unit((("https://example.test/iso4217", "USD"),), ()) == (
        "{https://example.test/iso4217}USD",
        None,
    )


def test_missing_numerator_is_rejected() -> None:
    with pytest.raises(ValueError, match="numerator"):
        canonicalize_xbrl_unit((), ((XBRLI, "shares"),))
