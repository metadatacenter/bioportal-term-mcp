# Roadmap

The work plan and status of `bioportal-term-mcp`. See [DESIGN.md](./DESIGN.md) for the
*why*; this file is the *what's left*.

## Tool status

Six tools planned, each mapped to a piece of the CEDAR controlled-term-field builder API.
Implementation status as of writing:

| Tool | Fills builder method | Status |
|---|---|---|
| `get_ontology(acronym)` | `withOntologyValueConstraint(uri, acronym, name)` | done |
| `get_class(class_iri, ontology_acronym)` | `withClassValueConstraint(...)` / `withBranchValueConstraint(...)` | done |
| `get_value_set(value_set_iri, vs_collection)` | `withValueSetValueConstraint(...)` | done |
| `find_class(query, ontology_acronym?)` | (free-text variant of `get_class`) | done |
| `find_ontology(query)` | (free-text variant of `get_ontology`) | done |
| `find_value_set(query)` | (free-text variant of `get_value_set`) | next |

Plus `ping(message)` for diagnostics.

## Build order, with rationale

Five of six tools done. One remaining: `find_value_set`. Same search-shape pattern as
`find_class` but scoped to value-set-collection ontologies (CEDARVS, HRAVS, etc.).

After all six exist, polish becomes worth doing systematically: add an HTTP cache layer
in `_bioportal_get` (TTL ~5 min), add an `_require_iri` validator for IRI-input tools,
maybe revisit async if latency matters.

## Known unimplemented patterns

These would be nice but aren't blocking:

- **Result caching.** Every tool currently hits BioPortal on every call. Same ontology
  looked up 5 times in one session = 5 HTTP calls. A TTL cache in `_bioportal_get` would
  fix this transparently. Easy to add when needed.
- **IRI input validation.** `get_class` and (future) `get_value_set` take IRIs but only
  check non-blank. A real URI parse-and-validate would catch typos client-side. Add an
  `_require_iri(value, field_name)` helper when implementing the value-set tool.
- **Find-tool pagination.** The `find_*` tools as designed return a single ranked page.
  BioPortal paginates by default; we'll request `pagesize=20-50` and let the orchestrating
  LLM ask for refinement rather than dumping pages.

## The bigger picture: this MCP plus its planned companion

This server is one half of a two-server architecture. The other half, `cedar-artifact-mcp`,
will:

- Expose the CEDAR artifact library's builders as tools (`create_template`, `add_field`,
  `add_ontology_constraint`, `validate`, `serialize`, etc.).
- Consume the IRI/acronym/name tuples that this server resolves.
- Validate via the library's readers.

The orchestrating LLM chains them: user describes a template in English → calls into this
MCP to resolve names → calls into `cedar-artifact-mcp` to build the artifact → validate →
return YAML/JSON.

**Why this MCP comes first**: it's independent of the CEDAR model evolution. The
`cedar-artifact-mcp` should wait until the CEDAR model lands its planned changes
(template/element merger), so it can target the *new* library and avoid a rewrite.

## Out of scope (do not add)

- **Anything that interprets natural language inside a tool.** See DESIGN.md Principle 1.
- **Generic BioPortal browsing / hierarchy walking.** This server is scoped to resolving
  terms for CEDAR controlled-term fields. "List children of class X" or "show me the
  ontology tree" don't belong here — BioPortal's web UI exists for that.
- **Support for non-BioPortal terminology servers.** If we ever need OLS or a private
  server, that's a separate MCP, not parameters here. See DESIGN.md Principle 3.
- **Server-side authentication beyond the BioPortal API key.** Single-key, single-tenant.
  If this ever needs multi-tenant auth, the deployment story changes substantially.

## Testing and CI (also not yet)

No CI is wired up. Worth adding eventually:

- GitHub Actions workflow running `uv run pytest` + `uv run pyright` on every push.
- A nightly cron running `uv run pytest -m live` to catch BioPortal shape drift early.
- Both before publishing to PyPI, if that's ever wanted.

## Decisions made along the way

Recorded here so they don't get relitigated in future sessions:

- **Python, not Java**, for this MCP. Anthropic's Python MCP SDK is the most mature; HTTP
  passthrough work doesn't benefit from JVM-locality. The artifact MCP will probably go
  Java for in-process library access.
- **`uv` as the package manager**, not pip or Poetry. Single binary, lockfile, fast.
- **Sync `httpx`**, not async. Simpler; latency hasn't been an issue. Revisit if it
  becomes one.
- **`respx` for HTTP mocking**, not unittest.mock patches. respx mocks at the transport
  layer (correct level) and gives readable request/response assertions.
- **Pydantic `BaseModel`** for outputs, not `TypedDict` or raw `dict`. The Field
  descriptions become part of the LLM-visible tool schema.
- **BSD-2-Clause license** (Stanford), to match the rest of the CEDAR ecosystem.
- **One server, one repo.** Even though the codebase is small, separate repo because:
  reusable beyond CEDAR, separate release cadence, no model-version coupling.
