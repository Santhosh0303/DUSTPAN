# Dustpan 0.1.2 repair ledger

Input: dustpan-0.1.1-source.zip, SHA-256
e1958403f0a65eab64ddc86701a5e6d046ee815d261b9e7fbeb8516eca2ea949.
The immutable prior final audit remains a historical NO-GO for 0.1.1.

## Authorized repair scope

The user authorized fixes for F01–F09 and explicitly deferred native and
Windows acceptance. This candidate is 0.1.2 to distinguish changed bytes;
GitHub tag availability and publication are still pending.

| Finding | Implemented repair | Evidence |
| --- | --- | --- |
| F01 | Coverage selects a model within its project; missing hints require one unambiguous deployment | X01; prior coverage controls |
| F02 | Decisions and checklist sets are computed before display escaping; identity keys remain raw | X02; metadata/public API controls |
| F03 | CLI and public writer resolve physical aliases and reject duplicate destinations before staging | X03; lexical/tilde controls |
| F04 | Root uses no-follow open with stat/fstat identity comparison | X04; final/intermediate component controls |
| F05 | Bounded read refuses size/timestamp changes and incomplete snapshots with a diagnostic | X05; file-budget controls |
| F06 | Known embedded documents validate JSON types and syntax irrespective of opening character | X06; malformed-query controls |
| F07 | Linux CI requires both 40-case matrices, six boundary regressions and 30 stress workloads | workflow and executable scripts |
| F08 | checkout, setup-python and upload-artifact use upstream-verified commit pins | refs recorded below |
| F09 | This current ledger, platform limitations and crash contract replace contradictory completion claims | README and CONTRACT |

The six repaired-boundary checks have passed. Ruff and strict mypy have passed.
Complete candidate matrix/artifact receipts accompany the repair handoff.
Do not interpret implemented fixes as Windows/native acceptance or publication
approval. Those gates remain deferred, including native-dependent matrix cases.

## Immutable action references

Verified using git ls-remote against each action's own GitHub repository:
- actions/checkout v4.2.2: 11bd71901bbe5b1630ceea73d27597364c9af683
- actions/setup-python v5.6.0: a26af69be951a213d495a4c3e4e4022e16d87065
- actions/upload-artifact v4.6.2: ea165f8d65b6e75b540449e92b4886f43607fa02

Pins establish immutable identity, not an assertion that upstream has no defects.

## Test semantics and historical evidence

The original native suite is retained. The six standalone regressions run
separately and do not inflate its test count. The immutable v3 input is included
under tests/audit_baseline solely for the existing history-integrity cases.
Matrix B now tests the exact declared backend floor, setuptools 77.0.3.

The low-level growing-file regression reconciles reader errors through the
pipeline before asserting material degradation, matching the reader API.
No failed safety assertion was removed to produce a green result.

Prism: skill used with manual security, adversarial, correctness, operations and
maintainability perspectives. Measurement tools unavailable; no score or
independent multi-model consensus claimed.
