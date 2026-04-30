#!/bin/bash
echo "=== MCP Diagnostics ==="
echo "Working directory: $(pwd)"
echo ""
echo "=== Configuration files ==="
ls -la .claude/
echo ""
echo "=== mcp.json ==="
cat .claude/mcp.json
echo ""
echo "=== settings.local.json ==="
cat .claude/settings.local.json
echo ""
echo "=== isa-lsp location ==="
which isa-lsp
echo ""
echo "=== isa-lsp version ==="
isa-lsp --version
echo ""
echo "=== Python location ==="
which python
echo ""
echo "=== Python version ==="
python --version
echo ""
echo "=== Installed packages ==="
pip list | grep -E "(fastmcp|pydantic|isa-lsp)"
echo ""
echo "=== Can import isa_lsp? ==="
python -c "import isa_lsp; print(f'✓ isa_lsp version {isa_lsp.__version__}')"
echo ""
echo "=== Isabelle available? ==="
which isabelle
isabelle version 2>/dev/null || echo "Isabelle not found in PATH"
echo ""
echo "=== Test MCP initialize ==="
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}' | timeout 5 isa-lsp 2>&1 | head -50
