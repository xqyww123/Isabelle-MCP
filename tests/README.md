# Isabelle-MCP Test Suite

Comprehensive test suite for the Isabelle LSP MCP server.

## Test Organization

### Unit Tests

Unit tests mock external dependencies and test individual components:

- **test_utils.py**: Core utility functions (errors, URI conversion, positions, formatters)
- **test_isabelle_tokens.py**: Symbol tokenizer and `find_symbol_occurrences`
- **test_lsp_client.py**: LSP client functionality (mocked)
- **test_tools_*.py**: Individual tool implementations
- **test_server.py**: MCP server wrapper functions
- **test_edge_cases.py**: Edge cases and error handling

### Integration Tests

Integration tests require a running Isabelle installation:

- **test_integration.py**: Full end-to-end tests with real Isabelle LSP server

### Test Files

```
tests/
├── conftest.py                      # Shared fixtures and configuration
├── test_utils.py                    # Utility function tests (42 tests)
├── test_isabelle_tokens.py          # Symbol tokenizer tests (39 tests)
├── test_lsp_client.py               # LSP client tests (38 tests)
├── test_tools_evaluate.py           # Evaluation tool + snapshot/render tests
├── test_tools_hover.py              # Hover tool tests (15 tests)
├── test_tools_definition.py         # Definition tool tests (16 tests)
├── test_tools_local_occurrences.py  # Local occurrences tool tests (7 tests)
├── test_tools_goal.py               # Goal tool tests (5 tests)
├── test_tools_command_output.py     # Command output tool tests (7 tests)
├── test_tools_session.py            # Session info tool tests (1 test)
├── test_server.py                   # Server wrapper tests (19 tests)
├── test_edge_cases.py               # Edge case tests (17 tests)
├── test_integration.py              # Integration tests (6 tests)
└── README.md                        # This file
```

**Total: 231 tests**

## Running Tests

### Quick Start

```bash
# Run all unit tests
pytest

# Run with coverage
pytest --cov=isabelle_mcp --cov-report=html
```

### Selective Test Execution

```bash
# Unit tests only (no Isabelle required)
pytest -m "not integration and not slow"

# Integration tests only (requires Isabelle)
pytest -m integration

# Slow tests only
pytest -m slow

# Specific file
pytest tests/test_utils.py

# Specific test class
pytest tests/test_utils.py::TestErrors

# Specific test
pytest tests/test_utils.py::TestErrors::test_isabelle_tool_error

# Verbose output
pytest -v

# Stop on first failure
pytest -x

# Show local variables on failure
pytest -l

# Run tests in parallel (requires pytest-xdist)
pytest -n auto
```

### Coverage Reports

```bash
# Generate HTML coverage report
pytest --cov=isabelle_mcp --cov-report=html

# View in browser
open htmlcov/index.html  # macOS
xdg-open htmlcov/index.html  # Linux

# Terminal coverage report
pytest --cov=isabelle_mcp --cov-report=term-missing

# Coverage for specific module
pytest --cov=isabelle_mcp.tools --cov-report=term
```

## Test Categories

### Markers

Tests are marked with pytest markers for selective execution:

- `@pytest.mark.integration` - Requires Isabelle installation
- `@pytest.mark.slow` - Takes significant time (e.g., session builds)
- `@pytest.mark.asyncio` - Async test (requires pytest-asyncio)

### Test Coverage Areas

#### 1. Utility Functions (42 tests) — `test_utils.py`
- Error handling and exceptions
- URI/file path conversion
- Position conversion (MCP ↔ LSP)
- HTML parsing and formatting
- Symbol extraction
- Edge cases and Unicode handling

#### 2. Symbol Tokenizer (39 tests) — `test_isabelle_tokens.py`
- Isabelle line tokenization (identifiers, named symbols, sub/superscripts)
- ASCII ↔ Unicode symbol matching
- `find_symbol_occurrences` column resolution

#### 3. LSP Client (38 tests) — `test_lsp_client.py`
- Client initialization
- JSON-RPC message handling
- Request/response management
- Document tracking
- Diagnostics caching
- Notification handling

#### 4. MCP Tools (70 tests) — `test_tools_*.py`
- Evaluation (evaluate_to / evaluation_status / cancel_evaluation)
- Hover information retrieval
- Go to definition
- Local occurrences (definition + uses)
- Diagnostics
- Proof goals
- Command output
- Session info

#### 5. Server Integration (19 tests) — `test_server.py`
- MCP wrapper functions
- Server lifespan management
- Tool dispatching

#### 6. Edge Cases (17 tests) — `test_edge_cases.py`
- File permissions
- Concurrency
- Memory handling
- Invalid input
- Unicode handling
- Race conditions
- Empty responses

#### 7. Integration Tests (6 tests) — `test_integration.py`
- Real Isabelle LSP interaction
- Document processing
- Error recovery

## Writing New Tests

### Test Structure

```python
import pytest
from isabelle_mcp.tools.my_tool import my_tool

class TestMyTool:
    """Test my_tool functionality."""

    @pytest.mark.asyncio
    async def test_basic_functionality(self, mock_lsp_client, temp_theory_file):
        """Test basic tool operation."""
        # Setup
        mock_lsp_client.some_response = expected_data

        # Execute
        result = await my_tool(mock_lsp_client, temp_theory_file, 1, 1)

        # Assert
        assert result is not None
        assert result.expected_field == expected_value

    @pytest.mark.asyncio
    async def test_error_handling(self, mock_lsp_client):
        """Test error conditions."""
        with pytest.raises(IsabelleToolError, match="expected error"):
            await my_tool(mock_lsp_client, "/nonexistent.thy", 1, 1)
```

### Available Fixtures

From `conftest.py`:

- `temp_theory_file`: Temporary valid Isabelle theory file
- `temp_theory_with_errors`: Temporary theory file with errors
- `mock_lsp_client`: Mock LSP client for unit tests
- `sample_hover_response`: Sample LSP hover response
- `sample_definition_response`: Sample LSP definition response
- `sample_highlights_response`: Sample LSP highlights response
- `sample_diagnostics`: Sample diagnostic messages

### Testing Async Functions

```python
@pytest.mark.asyncio
async def test_async_function():
    result = await async_function()
    assert result is not None
```

### Testing Error Conditions

```python
def test_validation_error():
    with pytest.raises(ValidationError):
        InvalidModel(bad_field="invalid")

def test_file_not_found():
    with pytest.raises(FileNotFoundError):
        function_that_reads_file("/nonexistent")

def test_isabelle_error():
    with pytest.raises(IsabelleToolError, match="expected message"):
        tool_that_fails()
```

### Parametrized Tests

```python
@pytest.mark.parametrize("input,expected", [
    (1, 2),
    (2, 4),
    (3, 6),
])
def test_double(input, expected):
    assert double(input) == expected
```

## Test Quality Guidelines

1. **Test one thing per test**: Each test should verify a single behavior
2. **Use descriptive names**: Test names should describe what they test
3. **Arrange-Act-Assert**: Structure tests with clear setup, execution, and verification
4. **Mock external dependencies**: Unit tests should not depend on external systems
5. **Test edge cases**: Include tests for boundary conditions and error paths
6. **Keep tests fast**: Unit tests should run in milliseconds
7. **Make tests deterministic**: Tests should produce same results every run
8. **Use fixtures**: Share common setup code via fixtures
9. **Document complex tests**: Add docstrings explaining non-obvious tests
10. **Maintain high coverage**: Aim for >80% code coverage

## Continuous Integration

For CI/CD pipelines:

```yaml
# Example GitHub Actions workflow
- name: Run tests
  run: |
    pip install -e ".[dev]"
    pytest -v --cov=isabelle_mcp --cov-report=xml

- name: Upload coverage
  uses: codecov/codecov-action@v3
```

## Debugging Tests

```bash
# Run with pdb on failure
pytest --pdb

# Run with verbose output
pytest -vv

# Show print statements
pytest -s

# Show local variables
pytest -l --tb=long

# Run specific test with maximum verbosity
pytest tests/test_utils.py::TestErrors::test_isabelle_tool_error -vv -s
```

## Performance Testing

```bash
# Show slowest tests
pytest --durations=10

# Profile test execution
pytest --profile

# Benchmark tests (requires pytest-benchmark)
pytest --benchmark-only
```

## Test Maintenance

- Run tests before committing: `pytest -x`
- Update tests when changing functionality
- Remove obsolete tests
- Keep fixtures up to date
- Monitor coverage trends
- Review and update edge cases

## Troubleshooting

### Common Issues

**Issue**: `ImportError: No module named 'isabelle_mcp'`
**Solution**: Install package in editable mode: `pip install -e .`

**Issue**: `pytest: command not found`
**Solution**: Install pytest: `pip install pytest pytest-asyncio`

**Issue**: Integration tests fail
**Solution**: Ensure Isabelle is installed: `isabelle version`

**Issue**: Coverage reports not generated
**Solution**: Install pytest-cov: `pip install pytest-cov`

**Issue**: Async tests fail
**Solution**: Install pytest-asyncio: `pip install pytest-asyncio`

## Resources

- [Pytest Documentation](https://docs.pytest.org/)
- [pytest-asyncio Documentation](https://pytest-asyncio.readthedocs.io/)
- [pytest-cov Documentation](https://pytest-cov.readthedocs.io/)
- [Testing Best Practices](https://docs.python-guide.org/writing/tests/)
