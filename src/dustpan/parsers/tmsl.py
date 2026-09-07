"""TMSL (`model.bim`) parser.

`model.bim` is the older, single-file JSON serialisation of a Tabular
Object Model (the same object model TMDL text files serialise across many
files). It shows up whenever a project has not been converted to the PBIP
`definition/` folder layout, so dustpan supports it directly rather than
requiring a conversion step.

Shape handled (Analysis Services JSON, "TMSL")::

    {
      "name": "Model", "compatibilityLevel": 1567,
      "model": {
        "culture": "en-US",
        "tables": [
          {"name": "Sales",
           "measures": [
             {"name": "Total Sales", "expression": "SUM(Sales[Amount])",
              "formatString": "#,0.00", "displayFolder": "Core",
              "isHidden": false, "description": "..."},
             {"name": "Multi-line", "expression": ["SUM (", "    Sales[Amount]", ")"]}
           ]}
        ]
      }
    }

The one genuinely tricky bit, and the reason this needs its own module
rather than reusing a generic JSON walker: **`expression` is either a JSON
string or a JSON array of strings**, the array form being how the Tabular
Object Model represents a multi-line DAX expression (one array element per
source line). Both are folded into the same newline-joined `str` before
they ever reach `dax.normalise` -- newlines are whitespace to the DAX
lexer, so the two forms normalise identically, which is exactly what
should happen for what is, on disk, the same measure written two ways.

Model identity: like `parsers/tmdl.py`, the semantic model's name comes
from `model_name_from_path` (the containing `*.SemanticModel` /
`*.Dataset` folder), **never** from the JSON's own top-level `"name"`. A
`model.bim`'s internal name is whatever the author typed into Tabular
Editor and can drift from the folder name; a report's `definition.pbir`
resolves its bound model from the folder path, too, so keeping every
`Metric.model` folder-derived is what lets `detect/unused.py` match a
report's references back to the right model at all. Trusting the JSON
name here would silently break that match for any model.bim whose
internal name differs from its folder -- not a hypothetical, since
Tabular Editor happily lets the two drift apart.

Never raises. A missing/wrong-typed field skips just that measure (or just
that property) and records why in `estate.errors`; one broken table or
measure never stops the rest of the file from being read.

Public surface (see CONTRACT.md):
    parse_model_bim(path: str, estate: Estate) -> None
"""

from __future__ import annotations

import io
import json
import os
from typing import Any

from dustpan import safeio

from ..ir import Estate, Metric, project_root
from .discover import model_name_from_path

__all__ = ["parse_model_bim"]

_STRING_MEASURE_FIELDS = {
    "formatString": "format_string",
    "displayFolder": "display_folder",
    "dataType": "data_type",
    "description": "description",
}


def parse_model_bim(path: str, estate: Estate) -> None:
    """Parse one `model.bim` (TMSL) file and append its measures.

    Never raises: a missing file, invalid JSON, or an unexpected shape is
    recorded in `estate.errors` and leaves the estate otherwise untouched;
    a malformed individual table or measure is skipped and noted without
    abandoning the rest of the file.
    """
    try:
        if not os.path.isfile(path):
            estate.errors.append(f"tmsl: {path}: not a file")
            return

        data = _read_json(path, estate)
        if data is None:
            return
        if not isinstance(data, dict):
            estate.errors.append(f"tmsl: {path}: top level is not a JSON object")
            return

        model_node = data.get("model")
        if not isinstance(model_node, dict):
            # Tolerate a bare model document (no outer name/compatibilityLevel
            # wrapper) as long as it has the one thing we actually need.
            model_node = data if isinstance(data.get("tables"), list) else None
        if model_node is None:
            estate.errors.append(
                f"tmsl: {path}: no 'model' object (and no top-level 'tables') -- "
                "not a recognised TMSL document"
            )
            return

        tables = model_node.get("tables")
        if tables is None:
            # A model with zero tables is unusual but not malformed.
            return
        if not isinstance(tables, list):
            estate.errors.append(f"tmsl: {path}: 'model.tables' is not a list")
            return

        model = model_name_from_path(path)
        for t_index, table in enumerate(tables):
            _parse_table(table, t_index, model, path, estate)
    except Exception as exc:  # pragma: no cover - defensive; contract: never raise
        estate.errors.append(f"tmsl: {path}: unexpected {type(exc).__name__}: {exc}")


def _read_json(path: str, estate: Estate) -> Any:
    try:
        text = safeio.read_text(path, estate, "tmsl")
        if text is None:
            return
        with io.StringIO(text) as fh:
            return json.load(fh)
    except FileNotFoundError:
        estate.errors.append(f"tmsl: missing file: {path}")
        return None
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        estate.errors.append(f"tmsl: unreadable JSON {path}: {exc}")
        return None


def _parse_table(table: Any, index: int, model: str, path: str, estate: Estate) -> None:
    if not isinstance(table, dict):
        estate.errors.append(f"tmsl: {path}: model.tables[{index}] is not an object")
        return

    raw_name = table.get("name")
    table_name = (
        raw_name.strip()
        if isinstance(raw_name, str) and raw_name.strip()
        else f"table{index}"
    )

    measures = table.get("measures")
    if measures is None:
        return
    if not isinstance(measures, list):
        estate.errors.append(f"tmsl: {path}: table '{table_name}'.measures is not a list")
        return

    for m_index, measure in enumerate(measures):
        metric = _parse_measure(measure, m_index, table_name, model, path, estate)
        if metric is not None:
            estate.metrics.append(metric)


def _parse_measure(
    measure: Any, index: int, table_name: str, model: str, path: str, estate: Estate
) -> Metric | None:
    if not isinstance(measure, dict):
        estate.errors.append(
            f"tmsl: {path}: table '{table_name}'.measures[{index}] is not an object"
        )
        return None

    raw_name = measure.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        estate.errors.append(
            f"tmsl: {path}: table '{table_name}'.measures[{index}] has no valid "
            "'name' -- skipped"
        )
        return None
    name = raw_name.strip()

    expression, expr_error = _coerce_expression(measure.get("expression"))
    if expr_error:
        estate.errors.append(
            f"tmsl: {path}: measure '{name}' in table '{table_name}': {expr_error}"
        )

    # AUD-V3-014: a recognised field holding the wrong TYPE used to be coerced
    # to None, which is exactly what a genuinely absent field looks like -- so
    # a measure whose formatString was a dict became "no format metadata", and
    # an equal-formula pair with no format metadata clears retirement. Invalid
    # is not absent: it is a reason to refuse, and it is recorded.
    invalid: list[str] = []
    fields: dict[str, str | None] = {}
    for json_key, metric_field in _STRING_MEASURE_FIELDS.items():
        if json_key not in measure:
            fields[metric_field] = None
            continue
        value = measure[json_key]
        if value is None or isinstance(value, str):
            fields[metric_field] = value or None
        else:
            fields[metric_field] = None
            invalid.append(json_key)
            estate.errors.append(
                f"tmsl: {path}: measure '{name}' in table '{table_name}': "
                f"'{json_key}' is a {type(value).__name__}, not a string. The "
                "value is unusable, so this measure's display metadata is "
                "treated as UNKNOWN rather than absent and cannot clear "
                "retirement."
            )

    # AUD-V1-008: TMSL carried `formatStringDefinition` straight past the
    # field map, so the SAME measure blocked retirement when read from TMDL
    # and cleared it when read from model.bim. A dynamic format string is a
    # separate DAX expression governing display; two measures with identical
    # value expressions can still show the user different things, so its mere
    # presence blocks interchangeability -- whichever parser found it.
    # Presence and shape, not truthiness: an empty or malformed
    # formatStringDefinition is not "no dynamic format" (AUD-V3-014).
    dynamic_format = False
    for key in ("formatStringDefinition", "formatStringDefinitionExpression"):
        if key not in measure:
            continue
        value = measure[key]
        dynamic_format = True
        well_formed = (
            (isinstance(value, str) and value != "")
            or (isinstance(value, dict) and bool(value))
            or (isinstance(value, list) and bool(value))
        )
        if not well_formed:
            invalid.append(key)
            estate.errors.append(
                f"tmsl: {path}: measure '{name}' in table '{table_name}': "
                f"'{key}' is present but empty or malformed. A dynamic format "
                "string that cannot be read blocks retirement rather than "
                "being treated as absent."
            )

    is_hidden_raw = measure.get("isHidden", False)
    is_hidden = (
        bool(is_hidden_raw) if isinstance(is_hidden_raw, bool) else _truthy(is_hidden_raw)
    )

    return Metric(
        id=f"powerbi:{model}:{name}",
        name=name,
        tool="powerbi",
        model=model,
        source_path=path,
        source_root=project_root(path),
        expression=expression,
        dialect="dax",
        description=fields["description"],
        display_folder=fields["display_folder"],
        format_string=fields["format_string"],
        data_type=fields["data_type"],
        is_hidden=is_hidden,
        extra=_measure_extra(table_name, dynamic_format, invalid),
    )


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in ("true", "1", "yes")
    return bool(value)


def _coerce_expression(value: Any) -> tuple[str, str | None]:
    """TMSL allows `expression` to be a string OR a list of source lines.

    Returns `(expression_text, error_or_None)`. On error the text is `""`
    rather than a guess -- an empty expression is what `dax.normalise`
    honestly reports as unparsable, which is the correct outcome for a
    measure whose expression dustpan could not read at all.
    """
    if isinstance(value, str):
        return value, None
    if isinstance(value, list):
        if not value:
            return "", "'expression' is an empty list"
        bad = next((type(x).__name__ for x in value if not isinstance(x, str)), None)
        if bad is not None:
            return "", f"'expression' list contains a non-string element ({bad})"
        return "\n".join(value), None
    if value is None:
        return "", "missing 'expression'"
    return "", f"'expression' has unexpected type {type(value).__name__}"


def _measure_extra(
    table_name: str, dynamic_format: bool, invalid: list[str]
) -> dict[str, object]:
    """Per-measure metadata carried alongside the parsed fields."""
    extra: dict[str, object] = {"table": table_name}
    if dynamic_format:
        extra["dynamic_format"] = True
    if invalid:
        extra["invalid_metadata"] = sorted(set(invalid))
    return extra
