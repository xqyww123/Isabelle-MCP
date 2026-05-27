# Isa-LSP Architecture

## System Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          AI Agent (Claude, etc.)                             │
│                     Processes NL → calls MCP tools → interprets results      │
└───────────────────────────────────┬─────────────────────────────────────────┘
                                    │ MCP Protocol (stdio, JSON-RPC)
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                          FastMCP Server (server.py)                          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌───────────────┐   │
│  │  Lifespan    │  │ Tool Router  │  │  Input       │  │ HTTP Hook     │   │
│  │  Management  │  │ & Registry   │  │  Validation  │  │ /notify-file- │   │
│  │              │  │ (10 tools)   │  │  (Pydantic)  │  │  change       │   │
│  └──────────────┘  └──────┬───────┘  └──────────────┘  └───────┬───────┘   │
└────────────────────────────┼────────────────────────────────────┼───────────┘
                             │                                    │
              ┌──────────────▼────────────────────────────────────▼───────────┐
              │                    Tool Layer (tools/*.py)                     │
              │                                                               │
              │  ┌─────────┐ ┌────────────┐ ┌────────────┐ ┌──────────────┐  │
              │  │  goal   │ │diagnostics │ │   hover    │ │ completions  │  │
              │  │ (PIDE   │ │ (cached    │ │ (LSP +     │ │ (LSP         │  │
              │  │  state) │ │  notifs)   │ │  PIDE)     │ │  standard)   │  │
              │  └─────────┘ └────────────┘ └────────────┘ └──────────────┘  │
              │  ┌─────────┐ ┌────────────┐ ┌────────────┐ ┌──────────────┐  │
              │  │  defn   │ │ highlights │ │  preview   │ │ cmd_output   │  │
              │  │ (goto   │ │ (symbol    │ │ (PIDE HTML │ │ (PIDE        │  │
              │  │  defn)  │ │  occurrences│ │  render)   │ │  dynamic)    │  │
              │  └─────────┘ └────────────┘ └────────────┘ └──────────────┘  │
              │  ┌──────────────┐  ┌──────────────┐                          │
              │  │ session_info │  │    build     │                          │
              │  │ (query state)│  │ (isabelle    │                          │
              │  │              │  │  build cmd)  │                          │
              │  └──────────────┘  └──────────────┘                          │
              └──────────────────────────────┬────────────────────────────────┘
                                             │
         ┌───────────────────────────────────▼───────────────────────────────┐
         │                  IsabelleLSPClient (lsp_client.py)                 │
         │                                                                   │
         │  ┌───────────────────┐  ┌───────────────────┐  ┌──────────────┐  │
         │  │ Process Lifecycle │  │ Request/Response   │  │  Document    │  │
         │  │ start/shutdown/   │  │ Correlation        │  │  State Mgmt  │  │
         │  │ crash recovery    │  │ pending_requests{} │  │  open_docs{} │  │
         │  └───────────────────┘  └───────────────────┘  └──────────────┘  │
         │                                                                   │
         │  ┌───────────────────┐  ┌───────────────────┐  ┌──────────────┐  │
         │  │ PIDE State Panel  │  │  Diagnostic       │  │  Concurrency │  │
         │  │ state_init/output │  │  Cache             │  │  _write_lock │  │
         │  │ /exit lifecycle   │  │  (per-file)        │  │  _caret_lock │  │
         │  └───────────────────┘  └───────────────────┘  └──────────────┘  │
         │                                                                   │
         │  ┌───────────────────────────────────────────────────────┐        │
         │  │            read_loop (async reader task)               │        │
         │  │  Dispatches: responses → Futures, notifs → handlers   │        │
         │  └───────────────────────────────────────────────────────┘        │
         └───────────────────────────────────┬───────────────────────────────┘
                                             │ JSON-RPC 2.0 over stdin/stdout
                                             │ (Content-Length framing)
                                             ▼
         ┌───────────────────────────────────────────────────────────────────┐
         │              isabelle vscode_server (Scala/JVM)                    │
         │                                                                   │
         │  ┌───────────────────┐  ┌───────────────────────────────────┐    │
         │  │  Standard LSP     │  │  PIDE Extensions                  │    │
         │  │  hover, completion│  │  caret_update, state_init/output  │    │
         │  │  definition, ...  │  │  decoration, dynamic_output       │    │
         │  └───────────────────┘  │  preview_request/response         │    │
         │                         └───────────────────────────────────┘    │
         └───────────────────────────────────┬───────────────────────────────┘
                                             │ PIDE Protocol
                                             ▼
         ┌───────────────────────────────────────────────────────────────────┐
         │                    Isabelle Prover Process                         │
         │          Theory processing, session heaps, proof engine            │
         └───────────────────────────────────────────────────────────────────┘
```

## Support Modules

```
┌──────────────────────────────────────────────────────────────────┐
│                    ProcessingTracker (processing.py)              │
│                                                                  │
│  Tracks PIDE decoration notifications to determine when          │
│  a document range has finished processing.                       │
│                                                                  │
│  PIDE/decoration ──► update() ──► range_processed(start, end)   │
│                                   wait_until_processed()         │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│                    FileWatcher (file_watcher.py)                  │
│                                                                  │
│  Detects dirty .thy/.ML files for re-sync on next tool call.     │
│                                                                  │
│  Sources:                                                        │
│    1. HTTP hook (/notify-file-change) ◄── external editors       │
│    2. inotify (watchdog) ◄── filesystem events                   │
│                                                                  │
│  Output: dirty_files set → consumed by sync_dirty_files()        │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│                    Utils                                          │
│                                                                  │
│  core.py:        MCPLine/LSPLine/MCPColumn/LSPCharacter          │
│                  (type-safe 1↔0 indexed position conversion)     │
│                                                                  │
│  formatters.py:  HTML→text, goal extraction, symbol extraction   │
│                  (BeautifulSoup-based parsing)                    │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│                    Models (models.py)                             │
│                                                                  │
│  Pydantic output models for each tool response:                  │
│  HoverInfo, CompletionsResult, DeclarationLocation,              │
│  HighlightsResult, DiagnosticsResult, GoalState,                 │
│  CommandOutputResult, PreviewResult, SessionInfo, BuildStatus     │
└──────────────────────────────────────────────────────────────────┘
```

## Data Flow: Goal Query (Most Complex Path)

```
Agent                    Server          LSP Client         vscode_server      Prover
  │                        │                │                    │               │
  │─isabelle_goal(line=42)─►                │                    │               │
  │                        │──open_document─►                    │               │
  │                        │                │──didOpen──────────►│               │
  │                        │                │                    │──load theory──►│
  │                        │                │◄─decoration─────── │◄──────────────│
  │                        │                │  (processing done) │               │
  │                        │                │                    │               │
  │                        │──get_goals─────►                    │               │
  │                        │  (before)      │──acquire caret_lock│               │
  │                        │                │──caret_update(0)──►│               │
  │                        │                │──state_init───────►│               │
  │                        │                │◄─state_output───── │◄─proof state──│
  │                        │                │──state_exit───────►│               │
  │                        │                │──release lock      │               │
  │                        │                │                    │               │
  │                        │──get_goals─────►                    │               │
  │                        │  (after)       │──acquire caret_lock│               │
  │                        │                │──caret_update(EOL)►│               │
  │                        │                │──state_init───────►│               │
  │                        │                │◄─state_output───── │◄─proof state──│
  │                        │                │──state_exit───────►│               │
  │                        │                │──release lock      │               │
  │                        │                │                    │               │
  │◄──GoalState────────────│                │                    │               │
  │  {goals_before, after} │                │                    │               │
```

## Concurrency Model

```
                     ┌─────────────────────────────┐
                     │      asyncio event loop      │
                     └──────────────┬──────────────┘
                                    │
            ┌───────────────────────┼───────────────────────┐
            │                       │                       │
    ┌───────▼───────┐      ┌───────▼───────┐      ┌───────▼───────┐
    │  Tool Handler │      │  read_loop    │      │  stderr_task  │
    │  (per request)│      │  (dispatches  │      │  (log stderr) │
    │               │      │   responses & │      │               │
    │  Acquires:    │      │   notifs)     │      └───────────────┘
    │  _caret_lock  │      └───────────────┘
    │  (for goals)  │
    └───────────────┘

    Locks:
    ┌────────────────┬──────────────────────────────────────────┐
    │ _write_lock    │ Protects stdin writes (message framing)  │
    │ _caret_lock    │ Serializes goal/output queries (global   │
    │                │ caret limitation in PIDE)                 │
    └────────────────┴──────────────────────────────────────────┘
```

## File Sync Flow

```
    External Editor                    Isa-LSP
    ┌───────────┐                ┌──────────────────┐
    │ saves     │──HTTP POST────►│ FileWatcher      │
    │ file.thy  │                │ _dirty_files += │
    └───────────┘                └────────┬─────────┘
         │                                │
         │──inotify event───────────────► │
         │                                │
         │                       Next tool call:
         │                       ┌────────▼─────────┐
         │                       │ sync_dirty_files()│
         │                       │ read file content │
         │                       │ didChange → LSP   │
         │                       └──────────────────┘
```
