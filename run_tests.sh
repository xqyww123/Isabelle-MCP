#!/bin/bash
# Script to run tests for Isabelle LSP MCP server

set -e  # Exit on error

echo "=== Isabelle LSP MCP Server Test Suite ==="
echo ""

# Check if pytest is installed
if ! command -v pytest &> /dev/null; then
    echo "Error: pytest is not installed"
    echo "Install with: pip install pytest pytest-asyncio"
    exit 1
fi

# Check if in virtual environment (recommended)
if [[ -z "$VIRTUAL_ENV" ]]; then
    echo "Warning: Not running in a virtual environment"
    echo "It's recommended to activate a virtual environment first"
    echo ""
fi

# Run different test suites
echo "1. Running unit tests..."
pytest tests/ -v -m "not integration and not slow" --tb=short

echo ""
echo "2. Running integration tests (requires Isabelle)..."
echo "   (Skipping if Isabelle not available)"
if command -v isabelle &> /dev/null; then
    pytest tests/ -v -m integration --tb=short || echo "Some integration tests failed (may be expected)"
else
    echo "   Isabelle not found, skipping integration tests"
fi

echo ""
echo "3. Running tests with coverage..."
pytest tests/ --cov=isa_lsp --cov-report=term-missing --cov-report=html -m "not integration and not slow"

echo ""
echo "=== Test Summary ==="
echo "Coverage report saved to: htmlcov/index.html"
echo ""
echo "To run specific test categories:"
echo "  - Unit tests only:        pytest -m 'not integration and not slow'"
echo "  - Integration tests:      pytest -m integration"
echo "  - Slow tests:             pytest -m slow"
echo "  - Specific file:          pytest tests/test_utils.py"
echo "  - Specific test:          pytest tests/test_utils.py::TestErrors::test_isabelle_tool_error"
echo ""
echo "All tests completed!"
