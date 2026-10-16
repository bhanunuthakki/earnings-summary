"""Pure XBRL measure coordinates shared with the isolated processor bundle."""

from __future__ import annotations

from typing import cast

UNIT_CONTRACT_VERSION = "xbrl-measures/v1"
_ISO4217 = "http://www.xbrl.org/2003/iso4217"
_XBRLI = "http://www.xbrl.org/2003/instance"

QName = tuple[str, str]


def _measure_name(measure: QName) -> str:
    namespace, name = measure
    if not namespace or not name or any(char in name for char in "{}*/()"):
        raise ValueError("XBRL unit measure QName is invalid")
    if namespace == _ISO4217 and len(name) == 3 and name.isascii() and name.isalpha():
        return name.upper()
    if namespace == _XBRLI and name in {"pure", "shares"}:
        return name
    return "{" + namespace + "}" + name


def canonicalize_xbrl_unit(
    numerator: tuple[QName, ...], denominator: tuple[QName, ...]
) -> tuple[str, str | None]:
    """Retain every measure and division term; source unit IDs are not semantic units."""

    if not numerator:
        raise ValueError("XBRL unit numerator is empty")
    top = tuple(sorted(_measure_name(measure) for measure in numerator))
    bottom = tuple(sorted(_measure_name(measure) for measure in denominator))
    unit_key = "*".join(top)
    if bottom:
        if len(top) > 1:
            unit_key = "(" + unit_key + ")"
        denominator_key = "*".join(bottom)
        if len(bottom) > 1:
            denominator_key = "(" + denominator_key + ")"
        unit_key += "/" + denominator_key
    currency = None
    if len(numerator) == 1:
        namespace, name = numerator[0]
        if namespace == _ISO4217 and len(name) == 3 and name.isascii() and name.isalpha():
            currency = name.upper()
    return unit_key, currency


def unit_coordinates_from_payload(payload: object) -> tuple[str, str | None]:
    """Reconstruct bridge coordinates from exact raw QName arrays, with no coercion."""

    if not isinstance(payload, dict):
        raise ValueError("XBRL unit measure evidence is invalid")
    evidence = cast(dict[object, object], payload)
    if set(evidence) != {"contract", "numerator", "denominator"}:
        raise ValueError("XBRL unit measure evidence is invalid")
    if evidence["contract"] != UNIT_CONTRACT_VERSION:
        raise ValueError("XBRL unit measure contract is unqualified")
    coordinates: list[tuple[QName, ...]] = []
    for key in ("numerator", "denominator"):
        values = evidence[key]
        if not isinstance(values, list):
            raise ValueError("XBRL unit measure array is invalid")
        measures: list[QName] = []
        for value in cast(list[object], values):
            if not isinstance(value, list):
                raise ValueError("XBRL unit measure QName is invalid")
            pair = cast(list[object], value)
            if len(pair) != 2 or not isinstance(pair[0], str) or not isinstance(pair[1], str):
                raise ValueError("XBRL unit measure QName is invalid")
            measures.append((pair[0], pair[1]))
        coordinates.append(tuple(measures))
    return canonicalize_xbrl_unit(coordinates[0], coordinates[1])
