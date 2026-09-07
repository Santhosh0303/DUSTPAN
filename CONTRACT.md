# dustpan build contract (FROZEN -- do not change signatures)

Python >= 3.11. **Standard library only.** No third-party dependencies, ever.
Target users run this on locked-down corporate Windows machines.

All modules code against `src/dustpan/ir.py`. Read it first.

## Files to build (single builder owns all of them)

- `src/dustpan/parsers/discover.py`, `parsers/tmdl.py`, `parsers/tmsl.py`, `parsers/report.py`
- `src/dustpan/dax/lexer.py`, `dax/normalise.py`
- `src/dustpan/detect/duplicates.py`, `detect/unused.py`
- `src/dustpan/pipeline.py`, `src/dustpan/cli.py`, `output/console.py`, `output/writers.py`
- `tests/test_parsers_model.py`, `tests/test_parsers_report.py`, `tests/test_dax.py`, `tests/test_detect.py`, `tests/test_cli.py`
- `README.md`

Public signatures are stable. Audited repairs may change internal behavior;
semantic source text must remain intact and display escaping must not decide safety.

## Required public signatures

```python
# parsers/discover.py
def discover(root: str) -> list[tuple[str, str]]:
    """Return [(kind, path)] where kind in {"pbip_model", "pbip_report", "model_bim"}."""

# parsers/tmdl.py
def parse_model(model_dir: str, estate: Estate) -> None: ...
# parsers/tmsl.py
def parse_model_bim(path: str, estate: Estate) -> None: ...
# parsers/report.py
def parse_report(report_dir: str, estate: Estate) -> None: ...

# dax/normalise.py
@dataclass
class NormResult:
    normalised: str
    fingerprint: str      # sha256 hex of normalised
    refs: list[Ref]
    agg_kind: str | None
    ok: bool
    error: str | None

def normalise(expression: str) -> NormResult: ...
def enrich(metric: Metric) -> None:
    """Run normalise() on metric.expression and populate the metric's
    normalised / fingerprint / refs / agg_kind / parse_ok / parse_error fields."""

# detect/duplicates.py
def find_duplicates(estate: Estate) -> list[Finding]: ...
# detect/unused.py
def find_unused(estate: Estate) -> list[Finding]: ...

# output/console.py
def render(estate: Estate) -> str: ...
# output/writers.py
def write_json(estate: Estate, path: str) -> None: ...
def write_markdown(estate: Estate, path: str) -> None: ...

# pipeline.py
def scan(root: str) -> Estate: ...
# cli.py
def main(argv: list[str] | None = None) -> int: ...
```

## Non-negotiable design rule

**Precision beats recall.** A false positive that makes someone delete a
working measure kills this product.

Three things used to be conflated in one `confidence` number (AUD-017).
They are separate and must stay separate:

| Axis | Question | Where it lives |
|---|---|---|
| **Certainty** | how sure is this statement? | `Finding.confidence` |
| **Coverage** | did we look at everything? | `ScanNote`, `HEALTH_KINDS` |
| **Actionability** | may a human act on it unreviewed? | `pipeline.proven_removable_metric_ids` |

`confidence == 1.0` means the statement is deterministic, not that it is
safe to act on. Two findings can both be 1.0 and mean different things: an
`exact_duplicate` (these expressions are identical) and an `unparsed` (these
measures definitively did not canonicalise). Neither is by itself permission
to delete anything.

Actionability is computed separately and requires ALL of: an exact duplicate
at 1.0; one deployment; interchangeable metadata; unambiguous identity; and
known usage coverage. Health findings (`HEALTH_KINDS`) are never filtered by
`--min-confidence`, and their presence must make a clean result impossible.

Parsers must never raise on malformed input -- append to `estate.errors`.

## File and output acceptance boundaries

Report coverage requires an unambiguous model/deployment within the project
root. A model hint selects only that model; without a hint, exactly one
deployment must exist. Sharing a parent directory does not cover sibling models.
Renderers decide actionability from the raw estate before making display copies.

POSIX reads use no-follow descriptor traversal including the root, verify root
identity at open, refuse non-regular files, and diagnose files changed during a
bounded read. This does not promise a transactionally consistent snapshot of a
whole actively changing directory tree. Windows local-disk reads hold ancestor
handles without write/delete sharing through file opening, reject every reparse
point, and transfer the final no-write/no-delete handle to a binary descriptor.
UNC paths and alternate streams are refused. Other platforms without guarded
reads emit material degradation. Windows acceptance requires its native CI gates.

Output aliases are rejected before staging. Each destination is replaced
atomically after staging and fsync. Existing bytes are copied into exclusive
recovery files, never moved away first. An ordinary commit failure attempts
rollback; failed restoration reports uncertain paths and retained recovery data.
An abrupt process exit can leave some destinations old and others new: there is
no multi-file crash transaction or automatic recovery replay. Directory fsync
is best effort, not a universal power-loss durability guarantee. Output parents
must not be concurrently replaced by another writer during a commit.

## Fixtures

`tests/fixtures/SalesDemo.SemanticModel/` and `SalesDemo.Report/` contain
planted cases:
- `Total Sales`, `Sales Total`, `Sum of Sales` -- 3 EXACT duplicates (must all be found)
- `Total Sales UK` -- adversarial, wraps the same SUM in CALCULATE (must NOT be a duplicate)
- `Revenue` -- SUMX over the same column (must NOT be an exact duplicate; near-match at most)
- `Orphan Measure` -- referenced by no visual (must be found as unused)
- `Total Sales`, `Revenue`, `Order Count` are referenced by report.json visuals
