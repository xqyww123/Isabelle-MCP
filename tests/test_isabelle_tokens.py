"""Unit tests for the Isabelle tokenizer and symbol occurrence finder."""

import pytest

from isa_lsp.utils.isabelle_tokens import find_symbol_occurrences, tokenize_isabelle_line


class TestTokenize:
    def test_simple_ident(self):
        tokens = tokenize_isabelle_line("hello")
        assert [t[0] for t in tokens] == ["hello"]

    def test_multiple_idents(self):
        tokens = tokenize_isabelle_line("P Q R")
        assert [t[0] for t in tokens] == ["P", "Q", "R"]

    def test_sub_ident(self):
        tokens = tokenize_isabelle_line("P\\<^sub>1")
        assert [t[0] for t in tokens] == ["P\\<^sub>1"]

    def test_named_symbol(self):
        tokens = tokenize_isabelle_line("P \\<Longrightarrow> Q")
        texts = [t[0] for t in tokens]
        assert texts == ["P", "\\<Longrightarrow>", "Q"]

    def test_long_ident(self):
        tokens = tokenize_isabelle_line("List.map")
        assert [t[0] for t in tokens] == ["List.map"]

    def test_type_var(self):
        tokens = tokenize_isabelle_line("'a")
        assert [t[0] for t in tokens] == ["'a"]

    def test_schematic_var(self):
        tokens = tokenize_isabelle_line("?x")
        assert [t[0] for t in tokens] == ["?x"]

    def test_schematic_var_with_index(self):
        tokens = tokenize_isabelle_line("?x.1")
        assert [t[0] for t in tokens] == ["?x.1"]

    def test_schematic_type_var(self):
        tokens = tokenize_isabelle_line("?'a")
        assert [t[0] for t in tokens] == ["?'a"]

    def test_number(self):
        tokens = tokenize_isabelle_line("42")
        assert [t[0] for t in tokens] == ["42"]

    def test_mixed(self):
        tokens = tokenize_isabelle_line("definition my_const :: nat where")
        texts = [t[0] for t in tokens]
        assert texts == ["definition", "my_const", ":", ":", "nat", "where"]

    def test_pod_not_split(self):
        """POD should not be split into individual letters."""
        tokens = tokenize_isabelle_line("POD")
        assert [t[0] for t in tokens] == ["POD"]

    def test_offsets_ascii(self):
        tokens = tokenize_isabelle_line("P \\<Longrightarrow> Q")
        assert tokens[0] == ("P", 0, 0)
        assert tokens[1][0] == "\\<Longrightarrow>"
        assert tokens[1][1] == 2
        assert tokens[2][0] == "Q"

    def test_sub_ident_full(self):
        tokens = tokenize_isabelle_line("P\\<^sub>1 \\<Longrightarrow> Q \\<Longrightarrow> P\\<^sub>1")
        texts = [t[0] for t in tokens]
        assert texts == ["P\\<^sub>1", "\\<Longrightarrow>", "Q", "\\<Longrightarrow>", "P\\<^sub>1"]

    def test_greek_letter(self):
        tokens = tokenize_isabelle_line("\\<alpha>")
        assert [t[0] for t in tokens] == ["\\<alpha>"]

    def test_greek_ident(self):
        tokens = tokenize_isabelle_line("\\<alpha>1")
        assert [t[0] for t in tokens] == ["\\<alpha>1"]

    def test_underscore_quote(self):
        tokens = tokenize_isabelle_line("my_func'")
        assert [t[0] for t in tokens] == ["my_func'"]

    def test_blanks_skipped(self):
        tokens = tokenize_isabelle_line("  a  b  ")
        assert [t[0] for t in tokens] == ["a", "b"]

    def test_empty(self):
        assert tokenize_isabelle_line("") == []

    def test_parens_and_operators(self):
        tokens = tokenize_isabelle_line("(f x)")
        texts = [t[0] for t in tokens]
        assert texts == ["(", "f", "x", ")"]

    def test_equals_operator(self):
        tokens = tokenize_isabelle_line("x = y")
        texts = [t[0] for t in tokens]
        assert texts == ["x", "=", "y"]

    def test_lambda_is_not_letter(self):
        """\\<lambda> is explicitly excluded from letter_symbols in symbol.ML."""
        tokens = tokenize_isabelle_line("\\<lambda>x. P x")
        texts = [t[0] for t in tokens]
        assert texts == ["\\<lambda>", "x", ".", "P", "x"]

    def test_lambda_search(self):
        """Searching for x should find both x's, not get swallowed by \\<lambda>."""
        offsets = find_symbol_occurrences("\\<lambda>x. P x", "x")
        assert len(offsets) == 2


class TestFindSymbolOccurrences:
    def test_single_match(self):
        offsets = find_symbol_occurrences("P Q R", "Q")
        assert offsets == [2]

    def test_no_match(self):
        offsets = find_symbol_occurrences("P Q R", "X")
        assert offsets == []

    def test_no_partial_match(self):
        """P should not match inside POD."""
        offsets = find_symbol_occurrences("POD P Q", "P")
        assert offsets == [4]

    def test_multiple_matches(self):
        offsets = find_symbol_occurrences("x = x", "x")
        assert offsets == [0, 4]

    def test_unicode_symbol_search(self):
        offsets = find_symbol_occurrences("P \\<Longrightarrow> Q", "⟹")
        assert len(offsets) == 1
        assert offsets[0] == 2

    def test_ascii_symbol_search(self):
        offsets = find_symbol_occurrences("P \\<Longrightarrow> Q", "\\<Longrightarrow>")
        assert len(offsets) == 1

    def test_unicode_file_unicode_search(self):
        offsets = find_symbol_occurrences("P ⟹ Q", "⟹")
        assert len(offsets) == 1
        assert offsets[0] == 2

    def test_unicode_file_ascii_search(self):
        offsets = find_symbol_occurrences("P ⟹ Q", "\\<Longrightarrow>")
        assert len(offsets) == 1
        assert offsets[0] == 2

    def test_sub_ident_not_partial(self):
        """Searching for P should not match P\\<^sub>1."""
        offsets = find_symbol_occurrences("P\\<^sub>1 Q P", "P")
        assert offsets == [12]

    def test_sub_ident_match(self):
        offsets = find_symbol_occurrences("P\\<^sub>1 Q P\\<^sub>1", "P\\<^sub>1")
        assert offsets == [0, 12]

    def test_multi_token_search(self):
        offsets = find_symbol_occurrences("P \\<Longrightarrow> Q \\<Longrightarrow> R", "Q \\<Longrightarrow> R")
        assert len(offsets) == 1

    def test_cap_at_10(self):
        line = " ".join(["x"] * 20)
        offsets = find_symbol_occurrences(line, "x")
        assert len(offsets) == 10

    def test_empty_line(self):
        assert find_symbol_occurrences("", "x") == []

    def test_empty_symbol(self):
        assert find_symbol_occurrences("hello", "") == []

    def test_long_ident_search(self):
        offsets = find_symbol_occurrences("List.map f xs", "List.map")
        assert offsets == [0]

    def test_type_var_search(self):
        offsets = find_symbol_occurrences("'a list", "'a")
        assert offsets == [0]
