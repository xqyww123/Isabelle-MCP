"""
Example Python script demonstrating direct usage of isa-lsp tools.

This shows how to use the MCP tools programmatically without an MCP client.
"""

import asyncio
from pathlib import Path

from isa_lsp.lsp_client import IsabelleLSPClient
from isa_lsp.tools import (
    completions,
    declaration_location,
    diagnostic_messages,
    document_highlights,
    goal,
    hover_info,
    session_info,
)


async def main():
    """Main example function."""
    print("=== Isabelle LSP MCP Server - Usage Example ===\n")

    # Get the path to example theory file
    examples_dir = Path(__file__).parent
    theory_file = examples_dir / "simple_theory.thy"

    if not theory_file.exists():
        print(f"Error: Theory file not found: {theory_file}")
        return

    # Create LSP client
    print("Starting Isabelle LSP client with HOL session...")
    client = IsabelleLSPClient(logic="HOL")
    await client.start()
    print("Client started successfully!\n")

    try:
        # Example 1: Get session info
        print("--- Example 1: Session Information ---")
        info = await session_info(client)
        print(f"Current session: {info.current_session}\n")

        # Example 2: Check diagnostics
        print("--- Example 2: Diagnostics ---")
        diags = await diagnostic_messages(client, str(theory_file))
        print(f"Processing complete: {diags.processing_complete}")
        print(f"Success (no errors): {diags.success}")
        print(f"Total diagnostics: {len(diags.items)}")

        if diags.items:
            print("\nDiagnostics:")
            for item in diags.items[:5]:  # Show first 5
                print(f"  [{item.severity}] Line {item.line}: {item.message}")
        print()

        # Give Isabelle time to process the file
        await asyncio.sleep(2)

        # Example 3: Hover info
        print("--- Example 3: Hover Information ---")
        # Query "my_const" at line 11 (definition line)
        hover = await hover_info(client, str(theory_file), 11, 15)
        print(f"Symbol: {hover.symbol}")
        print(f"Line context: {hover.line_context}")
        print(f"Info: {hover.info[:100]}..." if len(hover.info) > 100 else f"Info: {hover.info}")
        print()

        # Example 4: Completions
        print("--- Example 4: Completions ---")
        # Query completions at line 20 (empty line after definition)
        comps = await completions(client, str(theory_file), 20, 1, max_completions=10)
        print(f"Line context: {comps.line_context}")
        print(f"Completions found: {len(comps.items)}")
        if comps.items:
            print("\nTop completions:")
            for item in comps.items[:5]:
                print(f"  {item.label} ({item.kind}): {item.detail}")
        print()

        # Example 5: Go to definition
        print("--- Example 5: Go to Definition ---")
        # Query definition of "my_const" at its usage (line 18)
        defn = await declaration_location(client, str(theory_file), 18, 20)
        print(f"Symbol: {defn.symbol}")
        print(f"Definitions found: {len(defn.locations)}")
        for loc in defn.locations:
            print(f"  {loc.file_path}:{loc.line}:{loc.column}")
        print()

        # Example 6: Document highlights
        print("--- Example 6: Document Highlights ---")
        # Find all occurrences of "my_const"
        highlights = await document_highlights(client, str(theory_file), 11, 15)
        print(f"Symbol: {highlights.symbol}")
        print(f"Occurrences found: {len(highlights.highlights)}")
        for h in highlights.highlights[:5]:  # Show first 5
            print(f"  Line {h.line}, cols {h.start_column}-{h.end_column} ({h.kind})")
        print()

        # Example 7: Proof goals (MVP limitation - will return empty)
        print("--- Example 7: Proof Goals (MVP) ---")
        # Query goals at proof line
        state = await goal(client, str(theory_file), 28)
        print(f"Line context: {state.line_context}")
        print(f"Goals before: {state.goals_before}")
        print(f"Goals after: {state.goals_after}")
        print("Note: Goal queries return empty in MVP (PIDE state panel not implemented)")
        print()

        print("=== All examples completed successfully! ===")

    except Exception as e:
        print(f"Error during examples: {e}")
        import traceback
        traceback.print_exc()

    finally:
        # Cleanup
        print("\nShutting down LSP client...")
        await client.shutdown()
        print("Done!")


if __name__ == "__main__":
    asyncio.run(main())
