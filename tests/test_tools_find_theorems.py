"""Unit tests for find_theorems query serialization and output parsing.

These are prover-independent: they exercise the structured-criteria -> surface-query
serializer and the HTML output parser directly. The integration tests (needing the
patched prover) live under tests/integration.
"""

from pathlib import Path

import pytest

from isabelle_mcp.tools.find_theorems import serialize_find_theorems_query as serialize
from isabelle_mcp.utils import IsabelleToolError
from isabelle_mcp.utils.formatters import parse_find_theorems_from_html

_SAMPLE_HTML = Path(__file__).parent / "data" / "find_theorems_sample.html"


def _item(name: str, stmt: str) -> str:
    # Mirror the real make_html shape: <span class="item">...<a>name</a>:<break> stmt
    return (
        f'<span class="item"><span class="block">'
        f'<a href="file:/x#1">{name}</a>:<span class="break"> </span>{stmt}</span></span>'
    )


class TestSerializeBasics:
    def test_name_and_flags_and_pattern(self):
        q, note = serialize(names=["add"], intro=True, elim=False, patterns=["_ + _ = _ + _"])
        assert q == 'name: "add" intro -elim "_ + _ = _ + _"'
        assert note is None

    def test_exclude_and_simp(self):
        q, note = serialize(exclude_names=["Nat"], simp=["f x"])
        assert q == '-name: "Nat" simp: "f x"'
        assert note is None

    def test_tristate_none_omitted(self):
        q, _ = serialize(intro=None, elim=None, dest=None, solves=None)
        assert q == ""

    def test_all_flags_combination(self):
        q, _ = serialize(intro=True, elim=False, dest=True, solves=False)
        assert q == "intro -elim dest -solves"

    def test_exclude_patterns_and_simp(self):
        q, _ = serialize(exclude_patterns=["_ * 0"], exclude_simp=["g _"])
        assert q == '-"_ * 0" -simp: "g _"'

    def test_empty_query_when_no_criteria(self):
        q, note = serialize()
        assert q == ""
        assert note is None


class TestSerializeEdgeCases:
    @pytest.mark.parametrize("bad", [[""], ["   "], [None], ["", "  ", None]])
    def test_empty_and_whitespace_elements_dropped(self, bad):
        # F5: empty/whitespace/None elements collapse to the all-empty (list-all) query.
        assert serialize(patterns=bad)[0] == ""
        assert serialize(names=bad)[0] == ""
        assert serialize(simp=bad)[0] == ""

    def test_mixed_empty_and_real_elements(self):
        q, _ = serialize(patterns=["", "x = y", "  "])
        assert q == '"x = y"'

    def test_embedded_quote_is_escaped(self):
        # F3: an embedded double-quote must be escaped, not terminate the token.
        q, _ = serialize(patterns=['a "q" b'])
        assert q == r'"a \"q\" b"'

    def test_stray_backslash_is_escaped(self):
        # F3: a lone backslash (not an Isabelle symbol) must be escaped.
        q, _ = serialize(patterns=[r"a \ b"])
        assert q == r'"a \\ b"'

    def test_isabelle_symbol_backslash_preserved(self):
        # F3: \<in> is a single Isabelle symbol and must pass through unescaped.
        q, _ = serialize(patterns=[r"x \<in> S"])
        assert q == r'"x \<in> S"'

    def test_unicode_in_pattern_converted_with_note(self):
        # F4: ascii_of_unicode applies to patterns; a note flags the rewrite.
        q, note = serialize(patterns=["x ∈ S"])  # x ∈ S
        assert q == r'"x \<in> S"'
        assert note is not None and "ASCII" in note

    def test_unicode_in_name_not_converted(self):
        # F4: names are Parse.name literals, NOT inner-syntax terms; no conversion,
        # and no spurious rewrite note from a name.
        q, note = serialize(names=["foo"])
        assert q == 'name: "foo"'
        assert note is None


class TestParseTally:
    def test_found_with_displayed_truncation(self):
        # F7: "found N theorem(s) (M displayed):" — limited/truncated case.
        html = (
            '<pre class="source"><span class="writeln_message">'
            "find_theorems name: x<br/><br/>"
            "found 137 theorem(s) (5 displayed):"
            + _item("foo", "x = y") + _item("bar", "a ∧ b")
            + "</span></pre>"
        )
        found, displayed, thms = parse_find_theorems_from_html(html)
        assert found == 137
        assert displayed == 5
        assert [t[0] for t in thms] == ["foo", "bar"]
        assert thms[0][1] == "x = y"

    def test_found_without_truncation(self):
        html = (
            '<span class="writeln_message">found 2 theorem(s):'
            + _item("add.commute", "a + b = b + a")
            + _item("add.assoc", "a + b + c = a + (b + c)")
            + "</span>"
        )
        found, displayed, thms = parse_find_theorems_from_html(html)
        assert found == 2
        assert displayed == 2
        assert thms[0] == ("add.commute", "a + b = b + a")

    def test_displaying_no_limit(self):
        html = '<span class="writeln_message">displaying 3 theorem(s):' + _item("p", "q") + "</span>"
        found, displayed, _ = parse_find_theorems_from_html(html)
        assert found is None
        assert displayed == 3

    def test_real_wire_fixture(self):
        # Golden: parser on a real captured find_theorems wire payload.
        found, displayed, thms = parse_find_theorems_from_html(_SAMPLE_HTML.read_text())
        assert found == 9
        assert displayed == 2
        assert thms[0] == ("Groups.ab_semigroup_add_class.add_commute", "?a + ?b = ?b + ?a")
        assert thms[1][0] == "Groups.ab_semigroup_add.add_commute"

    def test_found_nothing(self):
        html = '<span class="writeln_message">find_theorems name: zzz<br/><br/>found nothing</span>'
        found, displayed, thms = parse_find_theorems_from_html(html)
        assert found == 0
        assert displayed == 0
        assert thms == []

    def test_empty_html(self):
        assert parse_find_theorems_from_html("") == (None, None, [])

    def test_criteria_echo_with_tally_text_not_mistaken(self):
        # tests-hygiene-3: a name criterion literally containing "found N theorem(s)"
        # is echoed before the real tally; the parser must read the REAL tally.
        html = (
            '<span class="writeln_message"><span class="keyword1">find_theorems</span> '
            'name: "found 5 theorem(s) here"<br/>'
            "found 9 theorem(s) (2 displayed):"
            + _item("A", "x = y") + _item("B", "a = b")
            + "</span>"
        )
        found, displayed, thms = parse_find_theorems_from_html(html)
        assert found == 9
        assert displayed == 2
        assert len(thms) == 2

    def test_criteria_echo_with_tally_text_then_found_nothing(self):
        html = (
            '<span class="writeln_message">'
            'find_theorems name: "found 12 theorem(s)"<br/>found nothing</span>'
        )
        found, displayed, thms = parse_find_theorems_from_html(html)
        assert found == 0
        assert displayed == 0
        assert thms == []


class TestParseError:
    def test_error_message_raises(self):
        # F6: a goal-requiring criterion at a non-proof caret embeds an error message;
        # it must surface as a tool error, not a bogus theorem.
        html = (
            '<pre class="source"><span class="error_message">'
            "Current goal required for intro search criterion"
            "</span></pre>"
        )
        with pytest.raises(IsabelleToolError, match="Current goal required"):
            parse_find_theorems_from_html(html)
