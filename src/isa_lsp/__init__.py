"""
Isa-LSP: Model Context Protocol Server for Isabelle

A Python MCP server that bridges AI agents with Isabelle's theorem prover
through its Language Server Protocol (LSP) implementation.
"""

__version__ = "0.1.0"
__author__ = "Isa-LSP Contributors"

from isa_lsp.utils.errors import IsabelleToolError

__all__ = [
    "__version__",
    "IsabelleToolError",
]
