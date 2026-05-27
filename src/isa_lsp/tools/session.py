from isa_lsp.lsp_client import IsabelleLSPClient
from isa_lsp.models import SessionInfo


async def session_info(client: IsabelleLSPClient) -> SessionInfo:
    return SessionInfo(current_session=client.logic)
