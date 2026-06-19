"""``isabelle_find_theorems`` — search the theorem database from a caret position.

Structured criteria are serialized into Isabelle's ``find_theorems`` surface
syntax (parsed by ``Find_Theorems.read_query``) and run, at the caret's context,
through the patched ``PIDE/find_theorems`` query operation. See
``docs`` and the plan for the design.
"""

from isabelle_mcp.evaluation import check_evaluation_guard, format_evaluation_result
from isabelle_mcp.lsp_client import IsabelleLSPClient
from isabelle_mcp.models import CommandSpan, EvaluationView, FindTheoremsResult, ThmEntry
from isabelle_mcp.utils import (
    IsabelleToolError,
    LSPCharacter,
    LSPLine,
    MCPLine,
    ascii_of_unicode,
    resolve_caret,
    symbol_explode,
)
from isabelle_mcp.utils.formatters import parse_find_theorems_from_html


def _clean(values: list[str] | None) -> list[str]:
    """Drop None / empty / whitespace-only elements from a criteria list."""
    if not values:
        return []
    return [v for v in values if v is not None and v.strip() != ""]


def _escape_for_string_token(value: str) -> str:
    """Escape a value for placement inside an Isabelle ``"..."`` string token.

    Symbol-aware: an Isabelle symbol such as ``\\<in>`` is a single symbol and must
    pass through untouched, but a lone ``"`` would terminate the token early and a
    lone ``\\`` would be read as a bad escape — so those two are escaped. Operating
    on ``symbol_explode`` output is what keeps ``\\<...>`` intact.
    """
    out: list[str] = []
    for sym in symbol_explode(value):
        if sym == '"':
            out.append('\\"')
        elif sym == "\\":
            out.append("\\\\")
        else:
            out.append(sym)
    return "".join(out)


def _term_token(value: str) -> str:
    """Quote an inner-syntax term (pattern/simp), converting Unicode to ASCII first."""
    return '"' + _escape_for_string_token(ascii_of_unicode(value)) + '"'


def _name_token(value: str) -> str:
    """Quote a fact-name token. Names are ``Parse.name`` literals (a substring/glob
    match), NOT inner-syntax terms, so no Unicode conversion is applied."""
    return '"' + _escape_for_string_token(value) + '"'


def serialize_find_theorems_query(
    *,
    names: list[str] | None = None,
    exclude_names: list[str] | None = None,
    intro: bool | None = None,
    elim: bool | None = None,
    dest: bool | None = None,
    solves: bool | None = None,
    patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    simp: list[str] | None = None,
    exclude_simp: list[str] | None = None,
) -> tuple[str, str | None]:
    """Serialize structured criteria into a find_theorems query string.

    Returns ``(query, note)`` where ``note`` flags any Unicode→ASCII rewriting.
    An all-empty set of criteria yields an empty query (lists all facts up to the
    limit), which is legal.
    """
    fragments: list[str] = []
    unicode_rewritten = False

    for n in _clean(names):
        fragments.append(f"name: {_name_token(n)}")
    for n in _clean(exclude_names):
        fragments.append(f"-name: {_name_token(n)}")

    for keyword, value in (("intro", intro), ("elim", elim), ("dest", dest), ("solves", solves)):
        if value is True:
            fragments.append(keyword)
        elif value is False:
            fragments.append(f"-{keyword}")

    def _emit_terms(values: list[str] | None, prefix: str) -> None:
        nonlocal unicode_rewritten
        for term in _clean(values):
            if ascii_of_unicode(term) != term:
                unicode_rewritten = True
            fragments.append(f"{prefix}{_term_token(term)}")

    _emit_terms(patterns, "")
    _emit_terms(exclude_patterns, "-")
    _emit_terms(simp, "simp: ")
    _emit_terms(exclude_simp, "-simp: ")

    note = (
        "Unicode glyphs in patterns/simp were rewritten to Isabelle ASCII notation."
        if unicode_rewritten
        else None
    )
    return " ".join(fragments), note


def _combine_notes(*notes: str | None) -> str | None:
    present = [n for n in notes if n]
    return " ".join(present) if present else None


async def find_theorems(
    client: IsabelleLSPClient,
    file_path: str,
    line: MCPLine,
    after_text: str | None = None,
    *,
    names: list[str] | None = None,
    exclude_names: list[str] | None = None,
    intro: bool | None = None,
    elim: bool | None = None,
    dest: bool | None = None,
    solves: bool | None = None,
    patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    simp: list[str] | None = None,
    exclude_simp: list[str] | None = None,
    limit: int | None = None,
    allow_duplicates: bool = False,
) -> FindTheoremsResult:
    if line < 1:
        raise IsabelleToolError(f"line must be >= 1, got {line}")

    await client.open_document(file_path)

    guard = await check_evaluation_guard(client, file_path, line)
    if isinstance(guard, EvaluationView):
        raise IsabelleToolError(format_evaluation_result(guard, client.project_root))
    guard_note = guard if isinstance(guard, str) else None

    doc = client.open_documents.get(file_path)
    if doc is None:
        raise IsabelleToolError(f"Document not open: {file_path}")
    lines = doc.content.split("\n")
    lsp_line_idx = int(line.to_lsp())
    caret_line, caret_char = resolve_caret(lines, lsp_line_idx, after_text, line)

    command = CommandSpan.from_lsp(
        await client.get_command_at_position(
            file_path, LSPLine(caret_line), LSPCharacter(caret_char),
        )
    )

    query, unicode_note = serialize_find_theorems_query(
        names=names, exclude_names=exclude_names,
        intro=intro, elim=elim, dest=dest, solves=solves,
        patterns=patterns, exclude_patterns=exclude_patterns,
        simp=simp, exclude_simp=exclude_simp,
    )
    limit_arg = str(limit) if limit else ""
    allow_dups_arg = str(allow_duplicates).lower()

    html = await client.get_find_theorems_at_position(
        file_path, LSPLine(caret_line), caret_char, query, limit_arg, allow_dups_arg,
    )
    if html is None:
        return FindTheoremsResult(
            command=command, found=None, displayed=None, theorems=[],
            note=_combine_notes(
                guard_note, unicode_note,
                "No command at the position; nothing was searched.",
            ),
        )

    # html is not None here, so the query DID run. If command is None, the caret was
    # on an ignored span (blank line / between commands / comment) and the search ran
    # in the preceding command's context (Isabelle walks backward to the nearest
    # non-ignored command). Flag that so command=None is not mistaken for
    # "nothing was searched" — including when that context yields no matches.
    between_commands_note = (
        "Caret is between commands; searched in the preceding command's context."
        if command is None
        else None
    )

    found, displayed, theorems = parse_find_theorems_from_html(html)
    return FindTheoremsResult(
        command=command,
        found=found,
        displayed=displayed,
        theorems=[ThmEntry(name=name, statement=stmt) for name, stmt in theorems],
        note=_combine_notes(guard_note, unicode_note, between_commands_note),
    )
