r"""Query tools (proof_state / find_theorems) at a position inside a `.ML` blob,
against a REAL prover.

Same wrong-node bug class as blob breakpoints: `query_command` paired the blob
file's OWN node with the loader theory's command (a blob position resolves, via
`current_command`, to the loader's `ML_file` command), so any query at a position
inside an `ML_file` blob answered `undefined` -- `Document.command_exec(blobNode,
loaderId)` raised.  Fixed by sending the command's own node through
`Language_Server.command_ref`.  A jar with the bug fails HERE: the query status
reads `undefined` instead of `ok`.

    PATH=contrib/Isabelle2025-2/bin:$PATH pytest tests/integration -m integration \
        -k blob_query
"""
import asyncio
import os
import shutil

import pytest

from isabelle_mcp import evaluation as ev
from isabelle_mcp.lsp_client import IsabelleLSPClient
from isabelle_mcp.utils import LSPLine

from .test_debugger_probes import _evaluate_through

pytestmark = pytest.mark.integration

if shutil.which("isabelle") is None:
    pytest.skip("isabelle not on PATH", allow_module_level=True)


THEORY = r'''theory BlobQ imports Main begin

ML_file "Aux.ML"

end
'''
BLOB = '''fun helper (n: int) = n + 1;
val _ = helper 3;
'''
ML_FILE_LINE = 3  # ML_file "Aux.ML"


@pytest.fixture
async def prover(tmp_path):
    ev.EVAL_POLL_INTERVAL = 4.0
    thy = os.path.join(str(tmp_path), "BlobQ.thy")
    blob = os.path.join(str(tmp_path), "Aux.ML")
    with open(thy, "w") as f:
        f.write(THEORY)
    with open(blob, "w") as f:
        f.write(BLOB)
    ev.evaluation_state = ev.EvaluationState()
    client = IsabelleLSPClient(
        logic="HOL",
        project_root=str(tmp_path),
        debug=True,
        extra_args=["-o", "editor_tracing_messages=0"],
    )
    await client.start()
    try:
        yield client, thy, blob
    finally:
        await client.shutdown()


async def _proof_state(client, blob, *, tries=30):
    """Any position inside the blob resolves to the loader's `ML_file` command;
    absorb the retryable `outdated` while the blob model is incorporated."""
    reply = None
    for _ in range(tries):
        reply = await client.get_proof_state_at_position(blob, LSPLine(0), 4)
        if reply.status != "outdated":
            break
        await asyncio.sleep(1.0)
    return reply


@pytest.mark.asyncio
async def test_query_at_a_blob_position_resolves_not_undefined(prover):
    client, thy, blob = prover
    assert await _evaluate_through(client, thy, ML_FILE_LINE), "the loader never evaluated"
    await client.open_document(blob)
    await asyncio.sleep(1.0)

    # The fix: the query now RESOLVES the loader command instead of raising on the
    # blob's own node.  proof_state on the ML_file command carries no goal, so
    # `no_proof_state` is the correct resolved answer; `undefined` was the exact
    # signature of the wrong-node bug.
    reply = await _proof_state(client, blob)
    assert reply is not None and reply.status != "undefined", (
        f"query at a blob position still hit the wrong-node bug: "
        f"{reply.status if reply else reply}"
    )

    # find_theorems in the blob's search context (the loader theory, Main visible)
    # is the positive witness: it resolves and returns results.
    ft = await client.get_find_theorems_at_position(
        blob, LSPLine(0), 4, query_text="add", limit="3", allow_dups="true")
    assert ft.status == "ok", f"find_theorems at a blob position failed: {ft.status}"
    assert ft.content, "find_theorems returned no content in the blob context"
