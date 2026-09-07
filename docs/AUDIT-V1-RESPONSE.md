> **SUPERSEDED — historical record.** This document reports the response to
> the **v1** audit and is preserved unchanged as evidence of what was claimed
> at that time. A later independent audit of v3 reopened several of the
> dispositions below. Do not read this as the current state: see
> `FIX-LEDGER.md` for the v3 remediation and its executed evidence. Old
> failure receipts are never rewritten into passes (v3 handoff, AUD-V3-025).

# Response to the v1 adversarial audit

Audit artifact: `dustpan-v1.zip`, SHA-256 `f4bc6200dc0a…c57fe`, 4 September 2026.
Result: 20 PASS / 20 FAIL, all 10 Critical cases failing.

Every finding below was reproduced locally before any code changed, and
re-run after. Findings not actioned are listed with the reasoning, not
quietly dropped.

## The three root causes

Eighteen findings collapse into three mistakes, and naming them is more
useful than the list.

**One decision, computed in three places.** Whether a duplicate set may
carry a retirement instruction was decided by the detector (deployment
match), by the pipeline (model *basename* match) and by each renderer
(confidence == 1.0). Each knew a different subset of the rules, so a set the
detector had already blocked was still printed as "Action: keep A, retire B".
`pipeline.apply_actionability` is now the only authority; nothing downstream
re-derives it.

**Guarantees checked at the boundary, not at the operation.** Scan-root
containment was verified once, on the targets discovery returned. PBIR opens
a tree of nested files afterwards, none of which were re-checked. The
overwrite guard *inferred* what had been read from `Metric.source_path`,
which misses every file that yields no Metric. Both are now enforced at the
single point where a read happens (`safeio.authorise`), and what was read is
recorded rather than reconstructed.

**Atomicity per file mistaken for atomicity per operation.** `_write` was
atomic for one path and was called once per destination, so a report and its
machine-readable twin could disagree. Outputs now stage, back up, commit, and
roll back together.

## Finding-by-finding

| ID | Severity | Status | Where |
|---|---|---|---|
| AUD-V1-001 | Critical | Fixed | `pipeline.apply_actionability`; renderers read `retirement_cleared` |
| AUD-V1-002 | Critical | Fixed | scope by `Metric.deployment`, never model basename |
| AUD-V1-003 | Critical | Fixed | any material scan note empties the removable set |
| AUD-V1-004 | Critical | Fixed | `Estate.read_paths` records every opened file |
| AUD-V1-005 | Critical | Fixed | `writers.commit_all` — stage, back up, commit, roll back |
| AUD-V1-006 | Critical | Fixed | `safeio.authorise` contains **every** read |
| AUD-V1-007 | High | Fixed | scrub on `Visual`/`Finding`/`ScanNote`; headings and summaries escaped |
| AUD-V1-008 | High | Fixed | TMSL `formatStringDefinition` retained and blocking |
| AUD-V1-009 | High | Fixed | depth cap records a material note |
| AUD-V1-010 | High | Fixed | `filter_by_confidence` carries notes, provenance and root |
| AUD-V1-011 | High | Fixed | rules selected explicitly; dev tools pinned to bounded ranges |
| AUD-V1-012 | High | Fixed | `os.walk` onerror + `_sorted_dirs` record denials |
| AUD-V1-013 | Medium | Fixed | summary counts deployments; names exposed separately |
| AUD-V1-014 | Medium | Fixed | releases built with `git archive`, not the working tree |
| AUD-V1-015 | Medium | Fixed | test count, IR version, identity section, licence metadata |
| AUD-V1-016 | Medium | **Open** | Windows unexecuted — see below |
| AUD-V1-017 | Low | **Partial** | Dependabot added; SHAs not pinned — see below |
| AUD-V1-018 | Low | Fixed | permission bits preserved; parent directory fsynced |

## Not fixed, and why

**AUD-V1-016 — Windows unproven.** Correct and unfixable here. The CI matrix
declares Windows on 3.11 and 3.12, but every result to date comes from Linux
and a green Linux run says nothing about path separators, console encoding
or symlink semantics on Windows. This stays a release gate; the first
Windows CI run is the receipt.

**AUD-V1-017 — mutable action tags.** Pinning to a full commit SHA requires
reading and verifying that SHA from the upstream repository, which was not
possible from the environment this tree was prepared in. Inventing a
plausible-looking SHA would be worse than an honest tag, so the workflow
keeps its tags with a comment saying exactly that, and `dependabot.yml` is
configured to maintain pins once they are set from a machine that can verify
them.

**AUD-V1-011 — the 58 violations were not reproduced verbatim.** Ruff 0.16.5
was not installable here; ruff 0.15.11 reported 3 against the old config. The
underlying cause is the real finding and is fixed: `ruff>=0.6` meant the gate
changed whenever a new linter shipped. Rules are now selected explicitly and
dev tools pinned to bounded ranges, so an upgrade cannot turn CI red without
a config change. Under the stricter explicit rule set the tree needed 25
further fixes, which were made.

## Matrix status

All 40 Edge/OAT cases are reimplemented in `tools/audit_matrix.py` against
their original expectations and run in CI:

| Severity | Tests | Passed | Failed | Pass rate |
|---|---:|---:|---:|---:|
| Critical | 10 | 10 | 0 | 100% |
| High | 10 | 10 | 0 | 100% |
| Medium | 10 | 10 | 0 | 100% |
| Low | 10 | 10 | 0 | 100% |
| **Total** | **40** | **40** | **0** | **100%** |

Two cases needed their premise restated, and both are stated here rather than
buried:

* **LO-05** originally checked that the *embedded* Git repository in the ZIP
  was structurally valid and hook-free. MO-03 now requires that no repository
  is embedded at all, so the original premise no longer exists. The case
  passes on absence, which satisfies the intent strictly more completely than
  a valid embedded repository would.
* **MO-05** required a final licence. `LICENSE` is now a real proprietary
  statement — all rights reserved, no grant — rather than a note saying it is
  a placeholder. That is the most restrictive possible position and can be
  replaced with anything else at any time; it is **not** a choice of licence
  made on the author's behalf, it is the removal of a non-statement.

Running the matrix from the SHIPPED ARCHIVE rather than the working tree
found three further problems, all of them in the harness rather than the
product, and all of the same shape -- a case that asserted a property of the
build environment instead of the artifact. `MO-03` and `LO-05` shelled out
to `release.sh`, which cannot run from an extracted copy with no repository;
`MO-03` then flagged `__pycache__` and `*.egg-info` that the harness's own
build cases had just written into the tree. A case that passes in a
development checkout and fails from the archive a user receives was
measuring the wrong thing.

Running the matrix also surfaced two defects in the product that the audit
had not: `Metric.id`
and `Asset.id` are composed from the raw measure name *before* scrubbing and
are rendered in evidence blocks, so terminal control bytes reached the
console through the id; and measure names interpolated inside Markdown code
spans were unescaped, so a name containing a backtick could terminate the
span and inject live Markdown after it. Both are fixed. A third, from `LE-02`:
a negative budget such as `DUSTPAN_MAX_FILE_BYTES=-5` silently disabled the
limit entirely, so a typo removed every cap -- only `0` disables now.

## Verification

- 280 tests (was 265); 15 new regression tests, one per finding, asserting
  the user-visible failure rather than the implementation shape
- strict `mypy` clean across 20 source files
- `ruff check` and `ruff format --check` clean under the explicit rule set
- DAX precision suite unchanged: 10 hostile pairs, zero false positives
- sdist runs its own tests from its extracted contents; wheel installs
  offline into a clean venv
