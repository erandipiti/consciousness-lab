"""Observation row validation (spec §9.3).

Arrow enforces types but not the cross-column rule that gives an observation its
meaning: **exactly one value column is non-null, and it is the one named by
``value_type``**. A row with two values set, or none, is not a device reading —
it is a corrupt record that would later be read as a measurement.
"""

from typing import Any

from consciousness_lab.session.model import (
    AppliesTo,
    ClaimStatus,
    ObservationKind,
    ObservationProvenance,
)

VALUE_COLUMNS = {
    "int64": "value_i64",
    "uint64": "value_u64",
    "float64": "value_f64",
    "decimal_string": "value_str",
}

_KINDS = {k.value for k in ObservationKind}
_APPLIES_TO = {a.value for a in AppliesTo}
_PROVENANCE = {p.value for p in ObservationProvenance}
_STATUS = {s.value for s in ClaimStatus}


class ObservationError(ValueError):
    """An observation row violates the §9.3 contract."""


def validate_observation(row: dict[str, Any]) -> None:
    """Raise if ``row`` is not a valid observation."""
    value_type = row.get("value_type")
    if value_type not in VALUE_COLUMNS:
        raise ObservationError(f"value_type {value_type!r} is not one of {sorted(VALUE_COLUMNS)}")
    if row.get("kind") not in _KINDS:
        raise ObservationError(f"kind {row.get('kind')!r} is not one of {sorted(_KINDS)}")
    if row.get("applies_to") not in _APPLIES_TO:
        raise ObservationError(f"applies_to {row.get('applies_to')!r} is not valid")
    if row.get("provenance") not in _PROVENANCE:
        raise ObservationError(f"provenance {row.get('provenance')!r} is not valid")
    if row.get("status") not in _STATUS:
        raise ObservationError(f"status {row.get('status')!r} is not valid")
    if not row.get("name"):
        raise ObservationError("an observation must be named")

    expected = VALUE_COLUMNS[value_type]
    populated = [column for column in VALUE_COLUMNS.values() if row.get(column) is not None]
    if populated != [expected]:
        raise ObservationError(
            f"value_type={value_type} requires exactly {expected} to be set, "
            f"but {populated or 'nothing'} is populated"
        )


def validate_observations(rows: list[dict[str, Any]]) -> None:
    for index, row in enumerate(rows):
        try:
            validate_observation(row)
        except ObservationError as exc:
            raise ObservationError(f"observation row {index}: {exc}") from exc
