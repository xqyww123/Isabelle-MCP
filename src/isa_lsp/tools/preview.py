from isa_lsp.lsp_client import IsabelleLSPClient
from isa_lsp.models import PreviewResult
from isa_lsp.utils import get_line_from_file, validate_position


async def preview_document(
    client: IsabelleLSPClient, file_path: str, line: int | None = None,
) -> PreviewResult:
    if line is not None:
        validate_position(line, 1)

    await client.open_document(file_path)
    if line is not None:
        await client.set_caret(file_path, line - 1)

    response = await client.request_preview(file_path)
    return PreviewResult(
        html=str(response.get("content", "")),
        line_context=get_line_from_file(file_path, line) if line is not None else None,
    )
