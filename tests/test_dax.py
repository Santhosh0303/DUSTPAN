"""Adversarial DAX lexer / normaliser tests.

This is the module the whole product hangs off (see `dax/normalise.py`'s
docstring), so it gets the most adversarial coverage in the suite. Two kinds
of pairs matter equally:

* MUST-MATCH pairs -- textually different, semantically identical DAX that
  has to collapse onto the same fingerprint, or dustpan misses a real
  duplicate (safe to miss, but a worse product).
* MUST-NOT-MATCH pairs -- textually similar DAX that must NOT collapse,
  because a false match here is the failure mode CONTRACT.md calls the one
  that kills the product: a false positive that gets a working measure
  deleted.

Every "must not match" case below is paired with a plain-English reason in
its id, so a failure here reads as a specific broken safety rule, not just
"assertion failed".
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from dustpan.dax.lexer import (
    KEYWORDS,
    TokenKind,
    ascii_upper,
    is_trivia,
    significant,
    tokenise,
)
from dustpan.dax.normalise import (
    UNPARSED_PREFIX,
    canonical_number,
    detect_agg_kind,
    enrich,
    extract_refs,
    normalise,
)
from dustpan.ir import Metric, Ref

# ---------------------------------------------------------------------------
# lexer
# ---------------------------------------------------------------------------


def _kinds(text: str) -> list[TokenKind]:
    return [t.kind for t in significant(tokenise(text))]


def test_lexer_never_raises_on_garbage():
    for bad in [
        "",
        None,
        123,
        "([{",
        '"unterminated',
        "'unterminated",
        "/* unterminated",
        "\x00\x01weird",
        "a" * 5000,
        "'''''''",
    ]:
        tokens = tokenise(bad)  # type: ignore[arg-type]
        assert isinstance(tokens, list)


def test_lexer_line_comment_slash_slash():
    tokens = significant(tokenise("1 // trailing\n+ 2"))
    assert [t.kind for t in tokens] == [
        TokenKind.NUMBER,
        TokenKind.OPERATOR,
        TokenKind.NUMBER,
    ]


def test_lexer_line_comment_double_dash():
    # -- starts a comment in DAX, so `1--2` is `1` then a comment, NOT `1 - -2`.
    tokens = significant(tokenise("1--2"))
    assert [t.kind for t in tokens] == [TokenKind.NUMBER]


def test_lexer_block_comment():
    tokens = significant(tokenise("1 /* mid\nblock */ + 2"))
    assert [t.kind for t in tokens] == [
        TokenKind.NUMBER,
        TokenKind.OPERATOR,
        TokenKind.NUMBER,
    ]


def test_lexer_unterminated_block_comment_is_error_not_exception():
    tokens = tokenise("1 + /* never closed")
    assert any(t.kind is TokenKind.ERROR for t in tokens)


def test_lexer_string_literal_with_doubled_quote_escape():
    tokens = significant(tokenise('"she said ""hi"""'))
    assert len(tokens) == 1
    assert tokens[0].kind is TokenKind.STRING
    assert tokens[0].value == 'she said "hi"'


def test_lexer_unterminated_string_is_error():
    tokens = tokenise('"never closed')
    assert any(t.kind is TokenKind.ERROR for t in tokens)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1", "1"),
        ("1.5", "1.5"),
        (".5", ".5"),
        ("1e3", "1e3"),
        ("1.5E-3", "1.5E-3"),
    ],
)
def test_lexer_number_forms(text, expected):
    tokens = significant(tokenise(text))
    assert len(tokens) == 1
    assert tokens[0].kind is TokenKind.NUMBER
    assert tokens[0].text == expected


@pytest.mark.parametrize(
    "text,table,column",
    [
        ("Sales[Amount]", "Sales", "Amount"),
        ("'Sales Data'[Amount]", "Sales Data", "Amount"),
        ("[Total Sales]", None, "Total Sales"),
        (
            "Sales /* x */ [Amount]",
            "Sales",
            "Amount",
        ),  # trivia between table and [ is ignored
    ],
)
def test_lexer_references(text, table, column):
    tokens = significant(tokenise(text))
    assert len(tokens) == 1
    ref = tokens[0]
    assert ref.kind is TokenKind.REF
    assert ref.table == table
    assert ref.column == column


def test_lexer_bracket_escape_for_literal_close_bracket():
    tokens = significant(tokenise("[Weird]]Name]"))
    assert len(tokens) == 1
    assert tokens[0].column == "Weird]Name"


def test_lexer_quoted_table_escape_for_literal_quote():
    tokens = significant(tokenise("'Bob''s Table'[Col]"))
    assert tokens[0].table == "Bob's Table"


@pytest.mark.parametrize(
    "op",
    [
        "<=",
        ">=",
        "<>",
        "==",
        "&&",
        "||",
        ":=",
        "&",
        "=",
        "<",
        ">",
        "+",
        "-",
        "*",
        "/",
        "^",
    ],
)
def test_lexer_operators(op):
    tokens = significant(tokenise(f"a {op} b"))
    ops = [t for t in tokens if t.kind is TokenKind.OPERATOR]
    assert len(ops) == 1
    assert ops[0].text == op


def test_lexer_var_return_are_keywords():
    tokens = significant(tokenise("VAR x = 1 RETURN x"))
    kinds_by_text = {t.text.upper(): t.kind for t in tokens}
    assert kinds_by_text["VAR"] is TokenKind.KEYWORD
    assert kinds_by_text["RETURN"] is TokenKind.KEYWORD
    assert "VAR" in KEYWORDS and "RETURN" in KEYWORDS


def test_lexer_dotted_function_name_is_one_token_not_var_dot_s():
    # VAR.S must lex as one IDENT, never as the VAR keyword followed by .S.
    tokens = significant(tokenise("VAR.S ( Sales[Amount] )"))
    assert tokens[0].kind is TokenKind.IDENT
    assert tokens[0].text == "VAR.S"


def test_ascii_upper_does_not_touch_non_ascii():
    # str.upper() would turn 'ß' into 'SS' -- a lossy, length-changing fold
    # that could merge two genuinely different names. ascii_upper must not:
    # every ASCII letter folds, 'ß' alone passes through untouched.
    assert ascii_upper("straße") == "STRAßE"
    assert ascii_upper("abcXYZ123") == "ABCXYZ123"


def test_is_trivia_and_significant_agree():
    tokens = tokenise("1 // c\n + 2")
    trivia = [t for t in tokens if is_trivia(t)]
    sig = significant(tokens)
    assert len(trivia) + len(sig) == len(tokens)
    assert all(t.kind in (TokenKind.WHITESPACE, TokenKind.LINE_COMMENT) for t in trivia)


# ---------------------------------------------------------------------------
# canonical_number
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1", "1"),
        ("1.0", "1"),
        ("1.00", "1"),
        ("1e0", "1"),
        (".5", "0.5"),
        ("0.50", "0.5"),
        ("10", "10"),
        ("1.230", "1.23"),
    ],
)
def test_canonical_number(text, expected):
    assert canonical_number(text) == expected


def test_canonical_number_uses_exact_decimal_never_float():
    # 0.1 + 0.2 != 0.3 in binary float; canonical_number must not launder a
    # numeral through float and risk changing the value it represents.
    text = "0.1"
    assert Decimal(canonical_number(text)) == Decimal("0.1")


# ---------------------------------------------------------------------------
# normalise(): MUST MATCH
# ---------------------------------------------------------------------------


MUST_MATCH = [
    ("whitespace", "SUM ( Sales[Amount] )", "SUM(Sales[Amount])"),
    ("quote-unification", "SUM('Sales'[Amount])", "SUM(Sales[Amount])"),
    ("line-comment-slash", "SUM(Sales[Amount]) // note", "SUM(Sales[Amount])"),
    ("line-comment-dash", "SUM(Sales[Amount]) -- note", "SUM(Sales[Amount])"),
    ("block-comment", "/* lead */ SUM(Sales[Amount])", "SUM(Sales[Amount])"),
    ("function-case", "sum(Sales[Amount])", "SUM(Sales[Amount])"),
    ("keyword-case", "var x = 1 return x", "VAR x = 1 RETURN x"),
    ("integer-float-1.0", "1.0 + Sales[Amount]", "1 + Sales[Amount]"),
    ("leading-dot-number", ".5 + Sales[Amount]", "0.5 + Sales[Amount]"),
    ("exponent-number", "1e0 + Sales[Amount]", "1 + Sales[Amount]"),
    ("trailing-zero-number", "1.50 + Sales[Amount]", "1.5 + Sales[Amount]"),
    ("multiline-layout", "SUM (\n\t'Sales'[Amount]\n)", "SUM(Sales[Amount])"),
]


@pytest.mark.parametrize("case_id,a,b", MUST_MATCH, ids=[c[0] for c in MUST_MATCH])
def test_must_match(case_id, a, b):
    ra, rb = normalise(a), normalise(b)
    assert ra.ok and rb.ok, f"expected both to parse: {ra.error!r} / {rb.error!r}"
    assert ra.fingerprint == rb.fingerprint, (
        f"{case_id}: expected same fingerprint for {a!r} and {b!r}, "
        f"got {ra.normalised!r} != {rb.normalised!r}"
    )


def test_fixture_duplicate_trio_matches_exactly():
    """The three planted exact-duplicate measures, verbatim from CONTRACT.md.

    This test is self-contained (hardcoded text); `test_parsers_model.py`
    covers the same trio parsed from the real fixture file end to end.
    """
    total_sales = "SUM ( Sales[Amount] )"
    sales_total = "SUM(Sales[Amount])"
    sum_of_sales = "SUM (\n\t\t\t\t'Sales'[Amount]\n\t\t\t)"
    results = [normalise(e) for e in (total_sales, sales_total, sum_of_sales)]
    assert all(r.ok for r in results)
    fingerprints = {r.fingerprint for r in results}
    assert len(fingerprints) == 1


# ---------------------------------------------------------------------------
# normalise(): MUST NOT MATCH
# ---------------------------------------------------------------------------


MUST_NOT_MATCH = [
    (
        "calculate-not-unwrapped",
        'CALCULATE(SUM(Sales[Amount]), Sales[Region]="UK")',
        "SUM(Sales[Amount])",
    ),
    (
        "sumx-not-rewritten-to-sum",
        "SUMX(Sales, Sales[Amount])",
        "SUM(Sales[Amount])",
    ),
    ("divide-args-not-sorted", "DIVIDE(a,b)", "DIVIDE(b,a)"),
    ("plus-args-not-sorted", "a + b", "b + a"),
    ("and-operands-not-sorted", "TRUE() && FALSE()", "FALSE() && TRUE()"),
    ("or-operands-not-sorted", "a || b", "b || a"),
    ("string-literal-case-preserved", 'IF([X]="UK", 1, 0)', 'IF([X]="uk", 1, 0)'),
    ("eq-vs-eqeq-distinct", "[X] = 1", "[X] == 1"),
    ("var-not-alpha-renamed", "VAR x = 1 RETURN x", "VAR y = 1 RETURN y"),
    ("count-vs-countrows", "COUNT(Sales[Amount])", "COUNTROWS(Sales)"),
    ("divide-vs-slash-operator", "DIVIDE(a, b)", "a / b"),
    ("and-vs-ampersand-ampersand", "AND(a, b)", "a && b"),
    ("different-numeric-value", "1", "2"),
    ("different-column", "Sales[Amount]", "Sales[Quantity]"),
    ("different-table-same-column", "Sales[Amount]", "Budget[Amount]"),
    ("measure-ref-vs-column-ref", "[Amount]", "Sales[Amount]"),
    # Bracket/quote content is taken verbatim, never trimmed: collapsing
    # "[ X ]" onto "[X]" would risk merging two references that (in
    # principle) name different objects. Under-normalising here is the safe
    # direction -- it only costs a missed match, never a false one.
    ("bracket-whitespace-is-significant-not-trimmed", "[ Total Sales ]", "[Total Sales]"),
]


@pytest.mark.parametrize(
    "case_id,a,b", MUST_NOT_MATCH, ids=[c[0] for c in MUST_NOT_MATCH]
)
def test_must_not_match(case_id, a, b):
    ra, rb = normalise(a), normalise(b)
    assert ra.fingerprint != rb.fingerprint, (
        f"{case_id}: {a!r} and {b!r} must NOT collide but both normalised to "
        f"{ra.normalised!r} -- this is exactly the false-positive failure mode "
        "CONTRACT.md forbids"
    )


def test_calculate_wrapped_measure_does_not_match_fixture_total_sales():
    total_sales = normalise("SUM ( Sales[Amount] )")
    total_sales_uk = normalise(
        'CALCULATE ( SUM ( Sales[Amount] ), Sales[Region] = "UK" )'
    )
    assert total_sales.ok and total_sales_uk.ok
    assert total_sales.fingerprint != total_sales_uk.fingerprint


def test_sumx_measure_does_not_exactly_match_fixture_total_sales():
    total_sales = normalise("SUM ( Sales[Amount] )")
    revenue = normalise("SUMX ( Sales, Sales[Amount] )")
    assert total_sales.ok and revenue.ok
    assert total_sales.fingerprint != revenue.fingerprint


# ---------------------------------------------------------------------------
# malformed expressions: never raise, always produce *something* comparable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "SUM(",
        "SUM(Sales[Amount]))",
        '"unterminated',
        "'unterminated",
        "[unterminated",
        None,
    ],
)
def test_malformed_expression_never_raises_and_reports_not_ok(bad):
    result = normalise(bad)  # type: ignore[arg-type]
    assert result.ok is False
    assert result.error
    assert result.fingerprint  # still deterministic, just marked unparsed
    assert result.normalised.startswith(UNPARSED_PREFIX)


def test_non_string_input_is_coerced_not_rejected_outright():
    # normalise() never raises on a non-str input, but "coerced" is not the
    # same as "always invalid": str(123) == "123" is perfectly valid DAX, so
    # it must parse -- the only hard guarantee is "never raises".
    result = normalise(123)  # type: ignore[arg-type]
    assert result.ok is True
    assert result.normalised == "123"


def test_two_identical_broken_expressions_still_agree_with_each_other():
    a = normalise("SUM(Sales[Amount]")
    b = normalise("SUM(Sales[Amount]")
    assert a.ok is False and b.ok is False
    assert a.fingerprint == b.fingerprint


def test_broken_expressions_with_different_text_still_differ():
    a = normalise("SUM(Sales[Amount]")
    b = normalise("AVERAGE(Sales[Amount]")
    assert a.fingerprint != b.fingerprint


def test_empty_expression_is_a_named_error_not_silently_valid():
    result = normalise("")
    assert result.ok is False
    assert "empty" in (result.error or "").lower()


def test_whitespace_only_expression_is_also_empty():
    result = normalise("   \n\t  ")
    assert result.ok is False


def test_unbalanced_parens_reported():
    result = normalise("SUM(Sales[Amount]")
    assert result.ok is False
    assert "(" in (result.error or "") or "unclosed" in (result.error or "").lower()


# ---------------------------------------------------------------------------
# extract_refs / detect_agg_kind
# ---------------------------------------------------------------------------


def test_extract_refs_dedupes_and_preserves_order():
    tokens = tokenise("Sales[Amount] + Sales[Amount] + Sales[Region]")
    refs = extract_refs(tokens)
    assert refs == [
        Ref(table="Sales", column="Amount"),
        Ref(table="Sales", column="Region"),
    ]


def test_extract_refs_bare_measure_has_none_table():
    refs = extract_refs(tokenise("[Total Sales]"))
    assert refs == [Ref(table=None, column="Total Sales")]


def test_extract_refs_ignores_table_only_mentions():
    # COUNTROWS(Sales) mentions the table "Sales" with no column -- must not
    # invent a phantom Ref for it.
    refs = extract_refs(tokenise("COUNTROWS(Sales)"))
    assert refs == []


def test_ref_fingerprint_is_order_independent():
    m1 = Metric(
        id="a",
        name="a",
        tool="powerbi",
        model="M",
        source_path="p",
        expression="",
        refs=[Ref("Sales", "Amount"), Ref("Sales", "Region")],
    )
    m2 = Metric(
        id="b",
        name="b",
        tool="powerbi",
        model="M",
        source_path="p",
        expression="",
        refs=[Ref("Sales", "Region"), Ref("Sales", "Amount")],
    )
    assert m1.ref_fingerprint() == m2.ref_fingerprint()


def test_ref_fingerprint_is_case_insensitive_via_key():
    assert Ref("Sales", "Amount").key() == Ref("SALES", "amount").key()


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("SUM(Sales[Amount])", "SUM"),
        ("SUMX(Sales, Sales[Amount])", "SUMX"),
        ('CALCULATE(SUM(Sales[Amount]), Sales[Region]="UK")', "SUM"),
        ("1 + 1", None),
        ("VAR x = 1 RETURN x", None),
        # Ambiguous outermost level (two different aggregations at the same
        # depth) must not guess -- returns None rather than picking one.
        ("DIVIDE(SUM(Sales[Amount]), COUNTROWS(Sales))", None),
    ],
)
def test_detect_agg_kind(expr, expected):
    assert detect_agg_kind(tokenise(expr)) == expected


# ---------------------------------------------------------------------------
# enrich()
# ---------------------------------------------------------------------------


def test_enrich_populates_all_fields_on_success():
    m = Metric(
        id="powerbi:M:X",
        name="X",
        tool="powerbi",
        model="M",
        source_path="p.tmdl",
        expression="SUM ( Sales[Amount] )",
        dialect="dax",
    )
    enrich(m)
    assert m.lexically_ok is True
    assert m.parse_error is None
    assert m.normalised == "SUM(SALES[AMOUNT])"
    assert m.fingerprint and len(m.fingerprint) == 64  # sha256 hex
    assert m.refs == [Ref("Sales", "Amount")]
    assert m.agg_kind == "SUM"


def test_enrich_marks_failure_without_raising():
    m = Metric(
        id="powerbi:M:Bad",
        name="Bad",
        tool="powerbi",
        model="M",
        source_path="p.tmdl",
        expression="SUM(Sales[Amount]",
        dialect="dax",
    )
    enrich(m)
    assert m.lexically_ok is False
    assert m.parse_error
    assert (
        m.fingerprint
    )  # still set -- unparsed measures still fingerprint deterministically


def test_enrich_skips_non_dax_dialect_honestly():
    m = Metric(
        id="powerbi:M:T",
        name="T",
        tool="tableau",
        model="M",
        source_path="p.twb",
        expression="SUM([Amount])",
        dialect="tableau-calc",
    )
    enrich(m)
    assert m.lexically_ok is False
    assert "tableau-calc" in (m.parse_error or "")


def test_enrich_never_raises_on_a_completely_broken_metric():
    m = Metric(
        id="x", name="x", tool="powerbi", model="M", source_path="p", expression=None
    )  # type: ignore[arg-type]
    enrich(m)  # must not raise
    assert m.lexically_ok is False


# ---------------------------------------------------------------------------
# round trip: the canonical string must re-lex to the same semantic content
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expr",
    [
        "SUM ( Sales[Amount] )",
        'CALCULATE(SUM(Sales[Amount]), Sales[Region]="UK")',
        "VAR x = SUM(Sales[Amount]) RETURN x + 1",
        "DIVIDE(SUM(Sales[Amount]), COUNTROWS(Sales), 0)",
    ],
)
def test_canonical_form_relexes_to_equivalent_references(expr):
    """The canonical string is meant to be re-lexable to the same ref set --
    the property that makes `_join`'s no-space rules safe (see its docstring).
    """
    result = normalise(expr)
    assert result.ok
    relexed = tokenise(result.normalised)
    assert not any(t.kind is TokenKind.ERROR for t in relexed)
    refs_before = extract_refs(tokenise(expr))
    refs_after = extract_refs(relexed)
    assert {r.key() for r in refs_before} == {r.key() for r in refs_after}
