from isabelle_mcp.lsp_client import IsabelleLSPClient
from isabelle_mcp.models import SessionInfo


async def session_info(client: IsabelleLSPClient) -> SessionInfo:
    version = client.isabelle_version
    return SessionInfo(
        current_session=client.logic,
        version=version if version and version != "unknown" else None,
    )
