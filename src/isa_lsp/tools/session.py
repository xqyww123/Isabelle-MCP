import asyncio
import logging

from isa_lsp.lsp_client import IsabelleLSPClient
from isa_lsp.models import BuildStatus, SessionInfo
from isa_lsp.utils import IsabelleToolError

logger = logging.getLogger(__name__)


async def session_info(client: IsabelleLSPClient) -> SessionInfo:
    # TODO: query actual available sessions via `isabelle build -n`
    return SessionInfo(
        current_session=client.logic,
        available_sessions=["Pure", "HOL", "HOL-Analysis", "HOL-Algebra", "HOL-Library", "Main", "ZF"],
    )


async def build_session(client: IsabelleLSPClient, session: str, clean: bool = False) -> BuildStatus:
    cmd = ["isabelle", "build", "-b"]
    if clean:
        cmd.append("-c")
    cmd.append(session)

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        output = stdout.decode('utf-8', errors='replace') + "\n" + stderr.decode('utf-8', errors='replace')
        messages = [line for line in output.split('\n') if line.strip()]
        return BuildStatus(success=process.returncode == 0, messages=messages, session=session)
    except Exception as exc:
        raise IsabelleToolError(f"Failed to build session '{session}': {exc}") from exc
