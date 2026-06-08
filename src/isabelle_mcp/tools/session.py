from isabelle_mcp.lsp_client import IsabelleLSPClient
from isabelle_mcp.models import SessionInfo


async def session_info(client: IsabelleLSPClient) -> SessionInfo:
    return SessionInfo(
        current_session=client.logic,
        version=client.isabelle_version or None,
    )
