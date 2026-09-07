"""Shared pytest fixtures for the dustpan test suite.

Everything here is read-only plumbing around the two planted fixture trees
(`tests/fixtures/SalesDemo.*`, `tests/fixtures/PbirDemo.Report`) described in
CONTRACT.md. Nothing here asserts anything itself -- see the `test_*.py`
files for that.
"""

from __future__ import annotations

import os

import pytest

from dustpan.dax.normalise import enrich
from dustpan.ir import Estate, Metric
from dustpan.parsers.report import parse_report
from dustpan.parsers.tmdl import parse_model

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
SALES_MODEL_DIR = os.path.join(FIXTURES_DIR, "SalesDemo.SemanticModel", "definition")
SALES_REPORT_DIR = os.path.join(FIXTURES_DIR, "SalesDemo.Report")
PBIR_REPORT_DIR = os.path.join(FIXTURES_DIR, "PbirDemo.Report")

# The seven measures CONTRACT.md plants in Sales.tmdl, and the roles they play.
DUPLICATE_TRIO = ("Total Sales", "Sales Total", "Sum of Sales")
ADVERSARIAL_CALCULATE = (
    "Total Sales UK"  # wraps the same SUM in CALCULATE -- must NOT match
)
ADVERSARIAL_SUMX = "Revenue"  # SUMX over the same column -- must NOT exact-match
ORPHAN = "Orphan Measure"  # referenced by no visual
REFERENCED_BY_REPORT = ("Total Sales", "Revenue", "Order Count")


def load_estate(*, model: bool = True, report: bool = True, pbir: bool = False) -> Estate:
    """Build an Estate from the fixture trees and enrich every metric.

    Mirrors what `pipeline.scan` does internally, but lets a test pick
    exactly which parts of the estate to populate (e.g. a model with no
    report at all, to exercise the "no usage evidence" path).
    """
    estate = Estate()
    if model:
        parse_model(SALES_MODEL_DIR, estate)
    if report:
        parse_report(SALES_REPORT_DIR, estate)
    if pbir:
        parse_report(PBIR_REPORT_DIR, estate)
    for metric in estate.metrics:
        enrich(metric)
    return estate


def metric_by_name(estate: Estate, name: str) -> Metric:
    for m in estate.metrics:
        if m.name == name:
            return m
    raise KeyError(
        f"no metric named {name!r} in estate ({[m.name for m in estate.metrics]!r})"
    )


def make_metric(
    name: str,
    expression: str,
    *,
    id: str | None = None,
    model: str = "M",
    is_hidden: bool = False,
    source_path: str = "test.tmdl",
) -> Metric:
    """A bare `Metric` with a real id, enriched via the real normaliser.

    Used by tests that want tight control over a handful of measures without
    parsing a whole file -- still exercises the real `dax.normalise` module,
    never hand-fabricated fingerprints.
    """
    metric = Metric(
        id=id or f"powerbi:{model}:{name}",
        name=name,
        tool="powerbi",
        model=model,
        source_path=source_path,
        expression=expression,
        dialect="dax",
        is_hidden=is_hidden,
    )
    enrich(metric)
    return metric


@pytest.fixture
def fixtures_dir() -> str:
    return FIXTURES_DIR


@pytest.fixture
def sales_model_dir() -> str:
    return SALES_MODEL_DIR


@pytest.fixture
def sales_report_dir() -> str:
    return SALES_REPORT_DIR


@pytest.fixture
def pbir_report_dir() -> str:
    return PBIR_REPORT_DIR


@pytest.fixture
def sales_estate() -> Estate:
    """SalesDemo model + its report, fully parsed and enriched."""
    return load_estate(model=True, report=True, pbir=False)


@pytest.fixture
def sales_estate_no_report() -> Estate:
    """SalesDemo model only -- no report/asset was ever scanned."""
    return load_estate(model=True, report=False, pbir=False)


@pytest.fixture
def full_estate() -> Estate:
    """SalesDemo model + both fixture reports (legacy and PBIR)."""
    return load_estate(model=True, report=True, pbir=True)
