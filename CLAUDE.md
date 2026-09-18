# For Claude (or any new contributor)

Start with these, in order:

1. **[README.md](./README.md)** — what this project is, how to install and run it, the
   six-tool inventory and current status.
2. **[DESIGN.md](./DESIGN.md)** — the architectural principles. Read this *before* adding
   tools, or you'll be tempted to put intelligence in the server that belongs in the
   orchestrating LLM.
3. **[CEDAR MCP Servers Roadmap](https://github.com/metadatacenter/cedar-development/blob/develop/ops/MCP-ROADMAP.md)** — what is left to do, here and across the
   four servers.

After those three, the code is self-explanatory. Patterns to mirror:

- Each tool: input validation via `_require_nonblank`, HTTP via `_bioportal_get`,
  Pydantic `BaseModel` for output, descriptive docstring (the LLM reads it).
- Tool docstrings and Pydantic Field descriptions must stay domain-agnostic. Do not
  reference downstream consumers — see DESIGN.md Principle 1.
- Each tool ships with: a happy-path mocked test, parametrized validation tests,
  error-path mocked tests, and one opt-in `@pytest.mark.live` test.
- See `get_class` for the canonical pattern across all of these.
