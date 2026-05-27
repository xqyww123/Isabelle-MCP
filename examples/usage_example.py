"""
Example Python script demonstrating direct usage of isa-lsp tools.

This shows how to use the MCP tools programmatically without an MCP client.
"""

import asyncio
from pathlib import Path

from isa_lsp.evaluation import evaluate_to, evaluation_status
from isa_lsp.lsp_client import IsabelleLSPClient
from isa_lsp.tools import (
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

    examples_dir = Path(__file__).parent
    theory_file = examples_dir / "simple_theory.thy"

    if not theory_file.exists():
        print(f"Error: Theory file not found: {theory_file}")
        return

    print("Starting Isabelle LSP client with HOL session...")
    client = IsabelleLSPClient(logic="HOL")
    await client.start()
    print("Client started successfully!\n")

    try:
        # Example 1: Session info
        print("--- Example 1: Session Information ---")
        info = await session_info(client)
        print(f"Current session: {info.current_session}\n")

        # Example 2: Evaluate the file
        print("--- Example 2: Evaluate File ---")
        result = await evaluate_to(client, str(theory_file), -1)
        while result.status == "in_progress":
            print(f"  Processing... (line {result.current_line})")
            if result.errors:
                for e in result.errors:
                    print(f"  [{e.severity}] Line {e.line}: {e.message}")
            result = await evaluation_status(client)
        print(f"  {result.message}\n")

        # Example 3: Diagnostics
        print("--- Example 3: Diagnostics ---")
        diags = await diagnostic_messages(client, str(theory_file), 1, -1)
        print(f"Success (no errors): {diags.success}")
        print(f"Total diagnostics: {len(diags.items)}")
        for item in diags.items[:5]:
            print(f"  [{item.severity}] Line {item.line}: {item.message}")
        print()

        # Example 4: Hover info
        print("--- Example 4: Hover Information ---")
        hover = await hover_info(client, str(theory_file), 11, 15)
        print(f"Symbol: {hover.symbol}")
        print(f"Info: {hover.info[:100]}..." if len(hover.info) > 100 else f"Info: {hover.info}")
        print()

        # Example 5: Go to definition
        print("--- Example 5: Go to Definition ---")
        defn = await declaration_location(client, str(theory_file), 18, 20)
        print(f"Symbol: {defn.symbol}")
        for loc in defn.locations:
            print(f"  {loc.file_path}:{loc.line}:{loc.column}")
        print()

        # Example 6: Document highlights
        print("--- Example 6: Document Highlights ---")
        highlights = await document_highlights(client, str(theory_file), 11, 15)
        print(f"Symbol: {highlights.symbol}")
        for h in highlights.highlights[:5]:
            print(f"  Line {h.line}, cols {h.start_column}-{h.end_column} ({h.kind})")
        print()

        # Example 7: Proof goals
        print("--- Example 7: Proof Goals ---")
        state = await goal(client, str(theory_file), 28)
        print(f"Line context: {state.line_context}")
        print(f"Goals before: {state.goals_before}")
        print(f"Goals after: {state.goals_after}")
        print()

        print("=== All examples completed successfully! ===")

    except Exception as e:
        print(f"Error during examples: {e}")
        import traceback
        traceback.print_exc()

    finally:
        print("\nShutting down LSP client...")
        await client.shutdown()
        print("Done!")


if __name__ == "__main__":
    asyncio.run(main())
