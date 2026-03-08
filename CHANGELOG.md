# Changelog

All notable changes to the Isabelle LSP MCP Server project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2024-XX-XX

### Added - MVP Release

#### Core Infrastructure
- LSP client wrapper (`lsp_client.py`) with full JSON-RPC 2.0 implementation
- Pydantic models for all tool inputs and outputs (`models.py`)
- Comprehensive utility modules:
  - Error handling with `IsabelleToolError`
  - URI conversion utilities
  - Position conversion (1-indexed MCP ↔ 0-indexed LSP)
  - HTML parsing and formatting utilities
- FastMCP server implementation with lifespan management

#### Standard LSP Tools (5 tools)
- `isabelle_hover`: Get type and documentation for symbols
- `isabelle_completions`: Get completion suggestions with relevance sorting
- `isabelle_definition`: Find symbol definitions (go to definition)
- `isabelle_highlights`: Find all occurrences of symbols
- `isabelle_diagnostics`: Get compiler errors and warnings

#### PIDE Extension Tools (3 tools)
- `isabelle_goal`: Query proof goals at positions (MVP with limitations)
- `isabelle_command_output`: Get prover output messages (MVP with limitations)
- `isabelle_preview`: Generate HTML preview of theories (MVP with limitations)

#### Session Management Tools (2 tools)
- `isabelle_session_info`: Get current session and available sessions
- `isabelle_build`: Build Isabelle session heap images

#### Documentation
- **SPECIFICATION.md**: Complete feature catalog and requirements (740 lines)
- **ARCHITECTURE.md**: System design and component descriptions (600+ lines)
- **API_DESIGN.md**: Detailed API specifications for all tools (900+ lines)
- **README.md**: Installation and usage guide with quick start
- **examples/**: Working examples with theory files and Python usage demos

#### Testing
- Unit tests for utilities (`test_utils.py`)
- Unit tests for Pydantic models (`test_models.py`)
- Integration tests for LSP client and tools (`test_integration.py`)
- Pytest configuration with integration and slow test markers

#### Examples
- `simple_theory.thy`: Basic theory file for testing
- `proof_example.thy`: Proof development examples
- `usage_example.py`: Python script demonstrating all tools
- `mcp_config.json`: Example MCP server configuration

### Known Limitations - MVP

The following features have limited functionality in this MVP release:

1. **isabelle_goal**: Returns empty goals
   - Requires PIDE state panel handler implementation
   - Full implementation tracked for Phase 2

2. **isabelle_command_output**: Returns empty messages
   - Requires dynamic output cache implementation
   - Full implementation tracked for Phase 2

3. **isabelle_preview**: Returns empty HTML
   - Requires preview notification handler
   - Full implementation tracked for Phase 2

4. **Session switching**: Requires LSP client restart
   - No hot-reload support in MVP
   - Client must be restarted with new session

5. **Available sessions**: Hardcoded list
   - Should query `isabelle build -n` dynamically
   - Enhancement tracked for Phase 2

### Design Decisions

- **1-indexed positions**: Following lean-lsp-mcp pattern for consistency
- **Pydantic models**: Structured outputs, never bare lists
- **Single LSP process**: Long-lived client for performance
- **Tool naming**: `isabelle_*` prefix for consistency
- **MVP scope**: Only LSP/PIDE native features, no command execution
- **Error handling**: Custom `IsabelleToolError` for all tool failures

### Technical Highlights

- Full JSON-RPC 2.0 over stdio implementation
- Asynchronous LSP client with background message reader
- Diagnostic caching with per-file tracking
- Document synchronization with open/close tracking
- Position conversion utilities for LSP ↔ MCP boundary
- HTML parsing for PIDE output extraction

## [Unreleased] - Future Enhancements

### Planned for Phase 2

#### PIDE Feature Completion
- State panel manager for goal queries
- Dynamic output cache for command output
- Preview response handler for HTML generation
- Session hot-reload without client restart

#### Enhanced Features
- Dynamic session discovery from `isabelle build -n`
- Streaming build output with progress updates
- Build cancellation support
- Heap image management and staleness detection

#### Performance Optimizations
- Request batching for multiple queries
- Caching of hover/completion results
- Incremental document updates
- Rate limiting for expensive operations

#### Additional Tools
- `isabelle_symbols`: List all symbols in document
- `isabelle_outline`: Get document structure (requires documentSymbol)
- `isabelle_format`: Format Isabelle code (requires formatting provider)
- `isabelle_search`: Search for theorems (requires command execution)

#### Developer Experience
- Type hints for all functions
- Comprehensive docstrings
- Performance benchmarks
- Load testing utilities
- Debug logging configuration

## Version History

- **0.1.0** - MVP release with core LSP tools and PIDE extensions

---

## Release Notes Format

For each release, we document:

- **Added**: New features and tools
- **Changed**: Changes to existing functionality
- **Deprecated**: Features marked for removal
- **Removed**: Removed features
- **Fixed**: Bug fixes
- **Security**: Security-related changes
- **Known Limitations**: Current constraints and workarounds
- **Design Decisions**: Rationale for key architectural choices
