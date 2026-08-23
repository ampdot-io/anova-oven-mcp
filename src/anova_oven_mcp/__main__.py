"""Command-line entry point for local stdio and future Pi HTTP deployment."""

from __future__ import annotations

import argparse


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Anova Precision Oven MCP server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
        help="Use stdio for desktop MCP hosts; HTTP is useful for a Pi service.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument(
        "--allow-lan",
        action="store_true",
        help="Explicitly allow an unauthenticated non-loopback HTTP bind.",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        from .server import create_server
    except ModuleNotFoundError as error:
        if error.name in {"mcp", "pydantic"}:
            raise SystemExit(
                "The MCP dependencies are not installed. Install 'anova-oven-mcp[mcp]' "
                "for control tools or 'anova-oven-mcp[server]' for camera support."
            ) from None
        raise
    server = create_server()
    if args.transport == "stdio":
        server.run("stdio")
        return
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not args.allow_lan:
        raise SystemExit(
            "Refusing a LAN bind without --allow-lan. Put authentication or a trusted "
            "reverse proxy in front of a Pi deployment."
        )
    server.run("streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
