"""Position-explicit query replies: the statuses the prover sends, and the
sentence each one becomes.

The prover answers with a status word and never with a sentence, because ML
cannot map a command back to a line and every sentence here names one. The
wording lives on this side, where it is unit-tested character for character and
where changing a word costs no jar rebuild. The status vocabulary is the same on
both sides — see ``scala/Isabelle2025-2/src/query.scala`` — and the design is
docs/archive/QUERY_TOOLS_UPGRADE.md §5.1 and §5.3.
"""

from __future__ import annotations

from dataclasses import dataclass

# The prelude's nine.
OK = "ok"
UNDEFINED = "undefined"
UNFINISHED = "unfinished"
INTERRUPTED = "interrupted"
NO_PROOF_STATE = "no_proof_state"
NO_CONTEXT = "no_context"
FAILED = "failed"
CANCELLED = "cancelled"
CRASHED = "crashed"

# The two only the adapter can observe.
NO_COMMAND = "no_command"
TIMEOUT = "timeout"

# Not a status: a rendering key. UNDEFINED has more than one cause and the prover
# cannot tell them apart, but the client sometimes can — it remembers whether the
# last evaluation was cancelled. When it knows, the reply names the cause; when it
# does not, it states the fact and stops. The instruction is the same either way,
# so the agent's next move never depends on which one it gets.
UNDEFINED_AFTER_CANCEL = "undefined_after_cancel"


@dataclass(frozen=True)
class QueryReply:
    """One answer to a position-explicit query.

    ``content`` is the rendered HTML when the status is ``ok``, the prover's own
    error text when it is ``failed``, and empty otherwise.
    """

    status: str
    comment: bool = False
    forked: bool = False
    content: str = ""


# Sentences that do not depend on what was asked for.
CANCELLED_MESSAGE = "The query was cancelled."
CRASHED_MESSAGE = "The prover could not answer this query and could not say why."
TIMEOUT_MESSAGE = "The prover did not answer this query within {seconds}s."

# Notes served alongside a result rather than instead of it.
COMMENT_NOTE = (
    "{where} is a comment or blank line; this is the proof state after the "
    "command before it."
)
FORKED_NOTE = (
    "This command forked work that is still running, so a failure may still "
    "surface at {where}."
)

# Sentences for a proof-state query.
PROOF_STATE_MESSAGES = {
    UNDEFINED: (
        "The prover no longer holds a proof state for the command at {where}. "
        "Evaluate the file again to get one."
    ),
    UNDEFINED_AFTER_CANCEL: (
        "The prover no longer holds a proof state for the command at {where} — "
        "the evaluation was cancelled. Evaluate the file again to get one."
    ),
    UNFINISHED: (
        "The command at {where} has not finished evaluating, so it has no proof "
        "state yet. Retry in a few seconds."
    ),
    INTERRUPTED: (
        "The evaluation of the command at {where} was interrupted, so it has no "
        "proof state. Evaluate the file again to get one."
    ),
    FAILED: "Reading the proof state at {where} failed: {message}",
    NO_PROOF_STATE: (
        "The command at {where} is not a proof operation, so there is no proof "
        "state here."
    ),
}


def notes(reply: QueryReply, where: str) -> list[str]:
    """The notes a served result carries, if any."""
    out = []
    if reply.comment:
        out.append(COMMENT_NOTE.format(where=where))
    if reply.forked:
        out.append(FORKED_NOTE.format(where=where))
    return out


def message(
    reply: QueryReply,
    where: str,
    seconds: float,
    messages: dict[str, str],
    *,
    after_cancel: bool = False,
) -> str:
    """The sentence for a status that cannot produce a result.

    *messages* selects the per-tool wording; the three that do not depend on what
    was asked for are shared. *after_cancel* says the client knows the last
    evaluation was cancelled, which lets one status name its cause. An
    unrecognised status is reported as a crash, which is what it is: the prover
    said something this side does not understand.
    """
    if reply.status == CANCELLED:
        return CANCELLED_MESSAGE
    if reply.status == TIMEOUT:
        return TIMEOUT_MESSAGE.format(seconds=int(seconds))
    template = None
    if after_cancel and reply.status == UNDEFINED:
        template = messages.get(UNDEFINED_AFTER_CANCEL)
    if template is None:
        template = messages.get(reply.status)
    if template is None:
        return CRASHED_MESSAGE
    return template.format(where=where, message=reply.content)
