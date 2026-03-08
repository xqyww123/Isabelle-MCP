"""
URI conversion utilities for LSP file paths.

LSP uses file:// URIs while MCP tools use absolute file paths.
"""

from pathlib import Path
from urllib.parse import quote, unquote


def file_path_to_uri(file_path: str) -> str:
    """Convert absolute file path to file:// URI.

    Args:
        file_path: Absolute file path (e.g., "/path/to/file.thy")

    Returns:
        file:// URI (e.g., "file:///path/to/file.thy")

    Examples:
        >>> file_path_to_uri("/path/to/file.thy")
        'file:///path/to/file.thy'

        >>> file_path_to_uri("/path/with spaces/file.thy")
        'file:///path/with%20spaces/file.thy'
    """
    # Resolve to absolute path
    path = Path(file_path).resolve()

    # Convert to URI with proper encoding
    # Use forward slashes even on Windows
    path_str = str(path).replace("\\", "/")

    # Encode special characters
    encoded_path = quote(path_str, safe="/:")

    return f"file://{encoded_path}"


def uri_to_file_path(uri: str) -> str:
    """Convert file:// URI to absolute file path.

    Args:
        uri: file:// URI (e.g., "file:///path/to/file.thy")

    Returns:
        Absolute file path (e.g., "/path/to/file.thy")

    Raises:
        ValueError: If URI doesn't start with "file://"

    Examples:
        >>> uri_to_file_path("file:///path/to/file.thy")
        '/path/to/file.thy'

        >>> uri_to_file_path("file:///path/with%20spaces/file.thy")
        '/path/with spaces/file.thy'

        >>> uri_to_file_path("http://example.com/file")
        Traceback (most recent call last):
        ...
        ValueError: Invalid file URI: http://example.com/file
    """
    if not uri.startswith("file://"):
        raise ValueError(f"Invalid file URI: {uri}")

    # Remove "file://" prefix and decode
    path = unquote(uri[7:])

    return path
