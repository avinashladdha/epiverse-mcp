"""
Epiverse MCP Server
===================
A Model Context Protocol (MCP) server that exposes the Epiverse R package
ecosystem to LLM clients (e.g. Claude Desktop).

Compatible with: mcp >= 2.0.0

Tools exposed:
  - list_packages          : List all R packages in the Epiverse directory
  - get_package_info       : Return DESCRIPTION + README summary for a package
  - search_r_docs          : Full-text search across man/ (.Rd) and vignettes/
  - read_source_code       : Read one or more .R / .Rd / .Rmd source files
  - list_package_functions : List every documented function in a package (from man/)
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import mcp.types as types
from mcp.server import InitializationOptions, Server
from mcp.server.stdio import stdio_server

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_default_root = Path.home() / "epiverse"
EPIVERSE_ROOT = Path(os.environ.get("EPIVERSE_ROOT", str(_default_root)))


def _discover_packages(root: Path) -> dict[str, Path]:
    """Return {package_name: package_path} for every R package under root."""
    packages: dict[str, Path] = {}
    for candidate in sorted(root.iterdir()):
        if candidate.is_dir() and (candidate / "DESCRIPTION").exists():
            packages[candidate.name] = candidate
    return packages


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _read_file_safe(path: Path, max_chars: int = 20_000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n[...truncated – file is {len(text):,} chars total]"
        return text
    except Exception as exc:
        return f"[Error reading {path}: {exc}]"


def _search_in_file(path: Path, pattern: re.Pattern) -> list[dict[str, Any]]:
    matches = []
    try:
        for i, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            if pattern.search(line):
                matches.append({"line": i, "text": line.rstrip()})
    except Exception:
        pass
    return matches


def _parse_description(pkg_path: Path) -> dict[str, str]:
    desc: dict[str, str] = {}
    desc_file = pkg_path / "DESCRIPTION"
    if not desc_file.exists():
        return desc
    current_key = ""
    for raw in desc_file.read_text(encoding="utf-8", errors="replace").splitlines():
        if raw and raw[0] not in (" ", "\t") and ":" in raw:
            key, _, value = raw.partition(":")
            current_key = key.strip()
            desc[current_key] = value.strip()
        elif current_key and raw.strip():
            desc[current_key] = desc.get(current_key, "") + " " + raw.strip()
    return desc


def _rd_to_plain(text: str) -> str:
    """Lightweight .Rd → plain-text (strips LaTeX-style markup)."""
    text = re.sub(r"\\[a-zA-Z]+\{([^}]*)\}", r"\1", text)
    text = re.sub(r"\\[a-zA-Z]+", " ", text)
    text = re.sub(r"[{}]", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Tool definitions (returned to the client on tools/list)
# ---------------------------------------------------------------------------

TOOLS = [
    types.Tool(
        name="list_packages",
        description=(
            "Returns the names and high-level descriptions of all Epiverse R packages "
            "available in the local repository. Call this first to orient yourself."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    types.Tool(
        name="get_package_info",
        description=(
            "Returns DESCRIPTION metadata and the first section of README.md for a "
            "specific Epiverse R package."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "package_name": {
                    "type": "string",
                    "description": "Exact package name, e.g. 'cfr' or 'cleanepi'.",
                }
            },
            "required": ["package_name"],
        },
    ),
    types.Tool(
        name="list_package_functions",
        description=(
            "Lists every documented function in a package by scanning man/*.Rd files. "
            "Returns function names with a one-line description and usage signature."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "package_name": {
                    "type": "string",
                    "description": "Exact package name, e.g. 'cfr'.",
                }
            },
            "required": ["package_name"],
        },
    ),
    types.Tool(
        name="search_r_docs",
        description=(
            "Full-text / regex search across man/ (.Rd files) AND vignettes/ (.Rmd files) "
            "for a keyword, function name, or concept. Returns matching file paths, line "
            "numbers, and up to 5 lines of context per file."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "Search term (plain text or regex), e.g. 'cfr_static' or 'delay_density'.",
                },
                "package_name": {
                    "type": "string",
                    "description": "Optional. Restrict search to a single package.",
                },
                "case_sensitive": {
                    "type": "boolean",
                    "description": "Whether the search is case-sensitive. Defaults to false.",
                },
            },
            "required": ["keyword"],
        },
    ),
    types.Tool(
        name="read_source_code",
        description=(
            "Reads the full contents of .R source files, .Rd documentation files, or "
            ".Rmd vignettes from a package. Supply relative paths within the package."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "package_name": {
                    "type": "string",
                    "description": "Exact package name, e.g. 'cfr'.",
                },
                "file_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Relative paths within the package, e.g. "
                        "['R/cfr_static.R', 'man/cfr_static.Rd']."
                    ),
                },
            },
            "required": ["package_name", "file_paths"],
        },
    ),
]


# ---------------------------------------------------------------------------
# Handler implementations
# ---------------------------------------------------------------------------

async def handle_list_tools(
    ctx: Any,
    params: types.PaginatedRequestParams | None,
) -> types.ListToolsResult:
    return types.ListToolsResult(tools=TOOLS)


async def handle_call_tool(
    ctx: Any,
    params: types.CallToolRequestParams,
) -> types.CallToolResult:
    name = params.name
    arguments: dict[str, Any] = params.arguments or {}
    packages = _discover_packages(EPIVERSE_ROOT)

    def text(s: str) -> types.CallToolResult:
        return types.CallToolResult(content=[types.TextContent(type="text", text=s)])

    # ── list_packages ──────────────────────────────────────────────────────
    if name == "list_packages":
        rows = ["# Epiverse R Packages\n"]
        for pkg_name, pkg_path in packages.items():
            desc = _parse_description(pkg_path)
            title = desc.get("Title", "No title available")
            version = desc.get("Version", "?")
            rows.append(f"## {pkg_name}  (v{version})")
            rows.append(f"   {title}\n")
        return text("\n".join(rows))

    # ── get_package_info ───────────────────────────────────────────────────
    elif name == "get_package_info":
        pkg_name = arguments.get("package_name", "")
        if pkg_name not in packages:
            return text(f"Package '{pkg_name}' not found. Use list_packages to see available packages.")
        pkg_path = packages[pkg_name]
        desc = _parse_description(pkg_path)
        sections = [f"# {pkg_name}\n"]
        for field in ["Title", "Version", "Description", "License", "URL", "BugReports"]:
            if field in desc:
                sections.append(f"**{field}:** {desc[field]}")
        if "Imports" in desc:
            sections.append(f"\n**Imports:** {desc['Imports']}")
        if "Suggests" in desc:
            sections.append(f"**Suggests:** {desc['Suggests']}")
        for readme_name in ("README.md", "README.Rmd"):
            readme = pkg_path / readme_name
            if readme.exists():
                excerpt = readme.read_text(encoding="utf-8", errors="replace")[:3000]
                sections.append(f"\n---\n## README (excerpt)\n\n{excerpt}\n")
                break
        return text("\n".join(sections))

    # ── list_package_functions ─────────────────────────────────────────────
    elif name == "list_package_functions":
        pkg_name = arguments.get("package_name", "")
        if pkg_name not in packages:
            return text(f"Package '{pkg_name}' not found.")
        man_dir = packages[pkg_name] / "man"
        if not man_dir.exists():
            return text(f"No man/ directory found in '{pkg_name}'.")
        rows = [f"# Functions in `{pkg_name}`\n"]
        for rd_file in sorted(man_dir.glob("*.Rd")):
            content = rd_file.read_text(encoding="utf-8", errors="replace")
            name_match = re.search(r"\\name\{([^}]+)\}", content)
            title_match = re.search(r"\\title\{([^}]+)\}", content)
            usage_match = re.search(r"\\usage\{([^}]+)\}", content, re.DOTALL)
            fn_name = name_match.group(1) if name_match else rd_file.stem
            fn_title = title_match.group(1).strip() if title_match else ""
            fn_usage = (
                usage_match.group(1).strip().split("\n")[0] if usage_match else ""
            )
            rows.append(f"### `{fn_name}`")
            if fn_title:
                rows.append(f"  {fn_title}")
            if fn_usage:
                rows.append(f"  Usage: `{fn_usage}`")
            rows.append(f"  Doc: `man/{rd_file.name}`\n")
        return text("\n".join(rows))

    # ── search_r_docs ──────────────────────────────────────────────────────
    elif name == "search_r_docs":
        keyword = arguments.get("keyword", "")
        pkg_filter = arguments.get("package_name")
        case_sensitive = arguments.get("case_sensitive", False)
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            pattern = re.compile(keyword, flags)
        except re.error:
            pattern = re.compile(re.escape(keyword), flags)

        search_pkgs = (
            {pkg_filter: packages[pkg_filter]}
            if pkg_filter and pkg_filter in packages
            else packages
        )

        results = [f"# Search results for: `{keyword}`\n"]
        total_hits = 0

        for pkg_name, pkg_path in search_pkgs.items():
            pkg_hits: list[str] = []
            man_dir = pkg_path / "man"
            if man_dir.exists():
                for rd_file in sorted(man_dir.glob("*.Rd")):
                    matches = _search_in_file(rd_file, pattern)
                    if matches:
                        rel = rd_file.relative_to(pkg_path)
                        pkg_hits.append(f"  **{rel}** ({len(matches)} match(es))")
                        for m in matches[:5]:
                            pkg_hits.append(f"    L{m['line']}: {m['text'][:120]}")
                        if len(matches) > 5:
                            pkg_hits.append(f"    ... and {len(matches)-5} more lines")
                        total_hits += len(matches)
            vignettes_dir = pkg_path / "vignettes"
            if vignettes_dir.exists():
                for vig_file in sorted(vignettes_dir.glob("*.Rmd")):
                    matches = _search_in_file(vig_file, pattern)
                    if matches:
                        rel = vig_file.relative_to(pkg_path)
                        pkg_hits.append(f"  **{rel}** ({len(matches)} match(es))")
                        for m in matches[:5]:
                            pkg_hits.append(f"    L{m['line']}: {m['text'][:120]}")
                        if len(matches) > 5:
                            pkg_hits.append(f"    ... and {len(matches)-5} more lines")
                        total_hits += len(matches)
            if pkg_hits:
                results.append(f"## {pkg_name}")
                results.extend(pkg_hits)
                results.append("")

        if total_hits == 0:
            results.append("No matches found. Try a broader keyword or check spelling.")
        else:
            results.append(f"\n---\n**Total matches: {total_hits}**")
        return text("\n".join(results))

    # ── read_source_code ───────────────────────────────────────────────────
    elif name == "read_source_code":
        pkg_name = arguments.get("package_name", "")
        file_paths: list[str] = arguments.get("file_paths", [])
        if pkg_name not in packages:
            return text(f"Package '{pkg_name}' not found.")
        pkg_path = packages[pkg_name]
        output: list[str] = []
        for rel_path in file_paths:
            full_path = pkg_path / rel_path
            output.append(f"\n{'='*60}")
            output.append(f"File: {pkg_name}/{rel_path}")
            output.append("=" * 60)
            if not full_path.exists():
                candidates = list(pkg_path.rglob(Path(rel_path).name))
                if candidates:
                    output.append(
                        f"[Exact path not found. Using: {candidates[0].relative_to(pkg_path)}]"
                    )
                    output.append(_read_file_safe(candidates[0]))
                else:
                    output.append(f"[File not found: {rel_path}]")
            else:
                content = _read_file_safe(full_path)
                if full_path.suffix == ".Rd":
                    output.append("--- Raw .Rd source ---")
                    output.append(content)
                    output.append("\n--- Plain text rendering ---")
                    output.append(_rd_to_plain(content))
                else:
                    output.append(content)
        return text("\n".join(output))

    else:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"Unknown tool: {name}")],
            is_error=True,
        )


# ---------------------------------------------------------------------------
# Server assembly and entry point
# ---------------------------------------------------------------------------

server = Server(
    name="epiverse-mcp",
    version="1.0.0",
    on_list_tools=handle_list_tools,
    on_call_tool=handle_call_tool,
)


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
