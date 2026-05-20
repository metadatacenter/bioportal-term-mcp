# bioportal-term-mcp

A focused [Model Context Protocol](https://modelcontextprotocol.io/) server that resolves
free-text descriptions of [BioPortal](https://bioportal.bioontology.org/) ontologies, classes,
and value sets into the canonical `(IRI, acronym, name, ...)` tuples required by the
[CEDAR artifact library](https://github.com/metadatacenter/cedar-artifact-library)'s
controlled-term-field builders.

Scope is deliberately narrow: this MCP exposes only what's needed to fill the arguments of
the four CEDAR `ControlledTermField` constraint methods. It is not a general-purpose
BioPortal client.

## Tools

Six tools, each mapping to a piece of the CEDAR controlled-term-field API:

| Tool | Fills builder method | Status |
|---|---|---|
| `get_ontology(acronym)` | `withOntologyValueConstraint(uri, acronym, name)` | implemented |
| `get_class(class_iri, ontology_acronym)` | `withClassValueConstraint(...)` / `withBranchValueConstraint(...)` | implemented |
| `get_value_set(value_set_iri, vs_collection)` | `withValueSetValueConstraint(...)` | implemented |
| `find_class(query, ontology_acronym?)` | (free-text variant of `get_class`) | implemented |
| `find_ontology(query)` | (free-text variant of `get_ontology`) | planned |
| `find_value_set(query)` | (free-text variant of `get_value_set`) | planned |

A diagnostic `ping(message)` tool is also exposed for round-trip verification.

## Requirements

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/) for dependency and venv management
- A [BioPortal API key](https://bioportal.bioontology.org/account) (free)

## Installation

```bash
git clone https://github.com/metadatacenter/bioportal-term-mcp.git
cd bioportal-term-mcp
uv sync                      # installs runtime + dev dependencies into .venv/
```

## Running

The server speaks MCP over stdio. Launch directly to confirm it starts:

```bash
BIOPORTAL_API_KEY=<your-key> uv run bioportal-term-mcp
```

The server will sit waiting for JSON-RPC messages on stdin. `Ctrl-C` to exit.

To use it from an MCP client (Claude Code, Claude Desktop, etc.), register it in the
client's MCP configuration. For Claude Code, edit `~/.claude.json`:

```json
{
  "mcpServers": {
    "bioportal-term": {
      "command": "/opt/homebrew/bin/uv",
      "args": [
        "--directory",
        "/absolute/path/to/bioportal-term-mcp",
        "run",
        "bioportal-term-mcp"
      ],
      "env": {
        "BIOPORTAL_API_KEY": "your-key-here"
      }
    }
  }
}
```

Notes:
- Use the absolute path returned by `which uv`. GUI clients don't inherit shell `PATH`.
- The `env` block is required — subprocesses don't inherit your shell's environment.
- Restart the MCP client after editing the config; servers are launched once per session.

## Development

```bash
uv sync --all-extras         # ensures dev dependencies are present
uv run pytest                # unit tests (no network, fast)
uv run pytest -v             # verbose
uv run pytest -m live        # opt-in: hits the real BioPortal API
uv run pyright               # static type-checking
```

The test suite uses [respx](https://lundberg.github.io/respx/) to mock all HTTP traffic.
Tests marked `@pytest.mark.live` are deselected by default and only run when explicitly
requested.

## Configuration

The server reads exactly one environment variable:

| Variable | Required | Description |
|---|---|---|
| `BIOPORTAL_API_KEY` | yes | BioPortal API key. Obtain from https://bioportal.bioontology.org/account. |

Missing or blank values cause every tool that needs the key to raise `RuntimeError` at
call time with a clear message.

## License

BSD-2-Clause. See [license.txt](./license.txt).
