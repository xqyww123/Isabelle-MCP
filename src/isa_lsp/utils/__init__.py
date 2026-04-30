"""
Utility modules for Isa-LSP MCP server.
"""

from isa_lsp.utils.errors import IsabelleToolError, check_pide_response
from isa_lsp.utils.formatters import (
    extract_symbol_from_lsp_range,
    extract_symbol_from_range,
    format_hover_content,
    get_line_from_file,
    parse_command_output_html,
    parse_goals_from_html,
    severity_int_to_string,
    strip_html_tags,
)
from isa_lsp.utils.positions import lsp_to_mcp_position, mcp_to_lsp_position, validate_position
from isa_lsp.utils.uri_utils import file_path_to_uri, uri_to_file_path

__all__ = [
    "IsabelleToolError",
    "check_pide_response",
    "file_path_to_uri",
    "uri_to_file_path",
    "mcp_to_lsp_position",
    "lsp_to_mcp_position",
    "strip_html_tags",
    "parse_goals_from_html",
    "parse_command_output_html",
    "get_line_from_file",
    "extract_symbol_from_range",
    "extract_symbol_from_lsp_range",
    "format_hover_content",
    "severity_int_to_string",
    "validate_position",
]
