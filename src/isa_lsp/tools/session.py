"""
Session management tools implementation.

These tools manage Isabelle session configuration and build status.
"""

import asyncio
from typing import Annotated

from pydantic import Field

from isa_lsp.lsp_client import IsabelleLSPClient
from isa_lsp.models import BuildStatus, SessionInfo
from isa_lsp.utils import IsabelleToolError


async def session_info(
    client: IsabelleLSPClient,
) -> SessionInfo:
    """Get information about current Isabelle session.

    Args:
        client: LSP client instance

    Returns:
        SessionInfo with session name and available sessions

    Note:
        This tool returns information about the LSP client's current session.
        The session is determined at client startup (default: HOL).
    """
    # Get current session from client
    current_session = client.logic

    # Get list of available sessions by querying Isabelle build
    # In MVP, we provide a hardcoded list of common sessions
    # TODO: Query actual available sessions using isabelle build -n
    available_sessions = [
        "Pure",
        "HOL",
        "HOL-Analysis",
        "HOL-Algebra",
        "HOL-Library",
        "Main",
        "ZF",
    ]

    return SessionInfo(
        current_session=current_session,
        available_sessions=available_sessions,
    )


async def build_session(
    client: IsabelleLSPClient,
    session: Annotated[str, Field(description="Session name to build (e.g., 'HOL', 'Main')")],
    clean: Annotated[bool, Field(description="Clean build (remove old heap images)")] = False,
) -> BuildStatus:
    """Build an Isabelle session to generate heap images.

    Args:
        client: LSP client instance
        session: Session name to build
        clean: Whether to clean before building

    Returns:
        BuildStatus with success flag and build messages

    Raises:
        IsabelleToolError: If build command fails

    Note:
        This tool invokes `isabelle build` as a subprocess. The build may
        take a long time for large sessions. In MVP, we use a simple
        subprocess call without streaming output.

        After building a session, you need to restart the LSP client with
        the new session to use it.
    """
    import logging

    logger = logging.getLogger(__name__)

    # Build command
    cmd = ["isabelle", "build", "-b"]

    if clean:
        cmd.append("-c")

    cmd.append(session)

    logger.info(f"Building session '{session}' with command: {' '.join(cmd)}")

    # Run build (this may take a long time)
    try:
        # Use asyncio subprocess for non-blocking execution
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await process.communicate()

        stdout_text = stdout.decode('utf-8', errors='replace')
        stderr_text = stderr.decode('utf-8', errors='replace')

        success = process.returncode == 0

        # Parse build output for errors/warnings
        messages = []
        output = stdout_text + "\n" + stderr_text

        for line in output.split('\n'):
            line = line.strip()
            if line:
                messages.append(line)

        if not success:
            logger.error(f"Session build failed with return code {process.returncode}")
            return BuildStatus(
                success=False,
                messages=messages,
                session=session,
            )

        logger.info(f"Session '{session}' built successfully")

        return BuildStatus(
            success=True,
            messages=messages,
            session=session,
        )

    except Exception as exc:
        raise IsabelleToolError(f"Failed to build session '{session}': {exc}") from exc


# ============================================================================
# NOTE: Future enhancements for session management
# ============================================================================
#
# 1. Query available sessions dynamically:
#    - Run `isabelle build -n` to get list of all sessions
#    - Parse ROOT files to get session dependencies
#    - Return structured session information
#
# 2. Stream build output:
#    - Use asyncio to stream build messages in real-time
#    - Provide progress updates for long builds
#    - Allow cancellation of running builds
#
# 3. Session switching:
#    - Implement tool to restart LSP client with different session
#    - Preserve open documents across restart
#    - Handle document reloading and re-processing
#
# 4. Heap management:
#    - Check heap image timestamps
#    - Detect when heaps are outdated
#    - Suggest rebuilds when needed
#
# These enhancements are beyond MVP scope but would improve UX significantly.
