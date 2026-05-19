# For Claude (or any new contributor)

Start with these, in order:

1. **[README.md](./README.md)** — what this project is, how to install and run it, the
   six-tool inventory and current status.
2. **[DESIGN.md](./DESIGN.md)** — the architectural principles. Read this *before* adding
   tools, or you'll be tempted to put intelligence in the server that belongs in the
   orchestrating LLM.
3. **[ROADMAP.md](./ROADMAP.md)** — what's done, what's next, what the relationship is
   to other planned MCPs (especially the future `cedar-artifact-mcp`).

After those three, the code is self-explanatory. Patterns to mirror:

- Each tool: input validation via `_require_nonblank`, HTTP via `_bioportal_get`,
  Pydantic `BaseModel` for output, descriptive docstring (the LLM reads it).
- Each tool ships with: a happy-path mocked test, parametrized validation tests,
  error-path mocked tests, and one opt-in `@pytest.mark.live` test.
- See `get_class` for the canonical pattern across all of these.
