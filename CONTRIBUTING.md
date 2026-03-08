# Contributing to Isabelle LSP MCP Server

Thank you for your interest in contributing to the Isabelle LSP MCP Server!

## Development Setup

### Prerequisites

- Python 3.10 or higher
- Isabelle 2024 or later
- Git for version control

### Installation for Development

1. Clone the repository:
```bash
cd contrib/Isa-LSP
```

2. Create a virtual environment:
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. Install in editable mode with dev dependencies:
```bash
pip install -e ".[dev]"
```

4. Verify installation:
```bash
isa-lsp --version
isabelle version
```

## Project Structure

```
Isa-LSP/
├── src/isa_lsp/          # Main package
│   ├── lsp_client.py     # LSP client wrapper
│   ├── models.py         # Pydantic models
│   ├── server.py         # MCP server
│   ├── instructions.py   # User-facing docs
│   ├── tools/            # MCP tool implementations
│   │   ├── hover.py
│   │   ├── completions.py
│   │   ├── definition.py
│   │   ├── highlights.py
│   │   ├── diagnostics.py
│   │   ├── goal.py
│   │   ├── command_output.py
│   │   ├── preview.py
│   │   └── session.py
│   └── utils/            # Utility modules
│       ├── errors.py
│       ├── uri_utils.py
│       ├── positions.py
│       └── formatters.py
├── tests/                # Test suite
│   ├── test_utils.py
│   ├── test_models.py
│   └── test_integration.py
├── docs/                 # Documentation
│   ├── SPECIFICATION.md
│   ├── ARCHITECTURE.md
│   └── API_DESIGN.md
├── examples/             # Example files
└── pyproject.toml        # Project configuration
```

## Coding Standards

### Python Style

We follow PEP 8 with some modifications:

- Line length: 100 characters (configured in Black)
- Use type hints for all functions
- Write docstrings for all public APIs
- Use async/await for all I/O operations

### Tools

- **Black**: Code formatting
- **Ruff**: Linting and import sorting
- **MyPy**: Static type checking
- **Pytest**: Testing framework

Run all checks:
```bash
# Format code
black src tests

# Lint
ruff check src tests

# Type check
mypy src

# Run tests
pytest
```

### Pre-commit Checks

Before committing, ensure:
1. All tests pass: `pytest`
2. Code is formatted: `black --check src tests`
3. No lint errors: `ruff check src tests`
4. Type checking passes: `mypy src`

## Testing

### Running Tests

```bash
# Run all tests
pytest

# Run unit tests only
pytest -m "not integration"

# Run integration tests (requires Isabelle)
pytest -m integration

# Run with coverage
pytest --cov=isa_lsp --cov-report=html
```

### Writing Tests

- Unit tests go in `tests/test_*.py`
- Mark integration tests with `@pytest.mark.integration`
- Mark slow tests with `@pytest.mark.slow`
- Use fixtures for common setup
- Aim for >80% code coverage

Example:
```python
@pytest.mark.asyncio
async def test_hover_tool():
    """Test hover tool with mock LSP client."""
    client = MockLSPClient()
    result = await hover_info(client, "/path/to/file.thy", 1, 1)
    assert result.symbol is not None
```

## Adding New Features

### Adding a New Tool

1. **Define the Pydantic model** in `src/isa_lsp/models.py`:
```python
class MyToolResult(BaseModel):
    """Result from my_tool."""
    data: str
    success: bool
```

2. **Implement the tool** in `src/isa_lsp/tools/my_tool.py`:
```python
async def my_tool(
    client: IsabelleLSPClient,
    file_path: str,
    line: int,
) -> MyToolResult:
    """Tool description."""
    # Implementation
    return MyToolResult(data="...", success=True)
```

3. **Export from tools package** in `src/isa_lsp/tools/__init__.py`:
```python
from isa_lsp.tools.my_tool import my_tool

__all__ = [..., "my_tool"]
```

4. **Register in server** in `src/isa_lsp/server.py`:
```python
@mcp.tool()
async def isabelle_my_tool(file_path: str, line: int):
    """Tool description."""
    return await my_tool(_lsp_client, file_path, line)
```

5. **Write tests** in `tests/test_my_tool.py`

6. **Update documentation**:
   - Add to SPECIFICATION.md
   - Add to API_DESIGN.md
   - Add to instructions.py
   - Add example usage

### Adding Utility Functions

1. Choose appropriate module in `src/isa_lsp/utils/`
2. Add function with type hints and docstring
3. Export from `utils/__init__.py`
4. Write unit tests
5. Update relevant documentation

## Documentation

### Docstring Format

Use Google-style docstrings:

```python
async def my_function(param1: str, param2: int) -> bool:
    """One-line summary.

    Longer description if needed.

    Args:
        param1: Description of param1
        param2: Description of param2

    Returns:
        Description of return value

    Raises:
        IsabelleToolError: When something fails
    """
```

### Updating Documentation

When making changes:

1. Update relevant docstrings
2. Update SPECIFICATION.md for feature changes
3. Update ARCHITECTURE.md for design changes
4. Update API_DESIGN.md for API changes
5. Update CHANGELOG.md
6. Add examples if needed

## Pull Request Process

1. **Create a feature branch**:
   ```bash
   git checkout -b feature/my-feature
   ```

2. **Make changes** following coding standards

3. **Write tests** for new functionality

4. **Update documentation** as needed

5. **Run all checks**:
   ```bash
   pytest
   black src tests
   ruff check src tests
   mypy src
   ```

6. **Commit changes** with clear messages:
   ```bash
   git commit -m "Add my_tool for XYZ functionality"
   ```

7. **Push and create PR**:
   ```bash
   git push origin feature/my-feature
   ```

8. **PR Description should include**:
   - What changes were made
   - Why they were needed
   - How to test them
   - Any breaking changes

## Release Process

1. Update version in `pyproject.toml`
2. Update CHANGELOG.md with release notes
3. Create git tag: `git tag v0.x.0`
4. Push tag: `git push origin v0.x.0`
5. Build and publish to PyPI (if applicable)

## Getting Help

- Check existing documentation in `docs/`
- Look at examples in `examples/`
- Review existing tests for patterns
- Ask questions in issues or discussions

## Code of Conduct

- Be respectful and inclusive
- Focus on constructive feedback
- Help others learn and grow
- Maintain professional communication

## License

By contributing, you agree that your contributions will be licensed under the same license as the project (MIT License).
