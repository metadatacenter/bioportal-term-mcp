# Roadmap

The work plan and status of `bioportal-term-mcp`. See [DESIGN.md](./DESIGN.md) for the
*why*; this file is the *what's left*.

## Tool status

Six tools across three resource types and two access modes:

|              | known identifier                              | free-text search                       |
|---           |---                                            |---                                     |
| **ontology** | `get_ontology(acronym)`                       | `find_ontology(query)`                 |
| **class**    | `get_class(class_iri, ontology_acronym)`      | `find_class(query, ontology_acronym?)` |
| **value set**| `get_value_set(value_set_iri, vs_collection)` | `find_value_set(query, vs_collection)` |

All six implemented. Plus `ping(message)` for diagnostics.

## Polish (none urgent)

Worth doing when motivation hits:

- **Result caching.** Every tool currently hits BioPortal on every call. Same ontology
  looked up 5 times in one session = 5 HTTP calls. A TTL cache in `_bioportal_get` would
  fix this transparently. Easy to add when needed.
- **IRI input validation.** `get_class` and `get_value_set` take IRIs but only check
  non-blank. A real URI parse-and-validate would catch typos client-side. Add an
  `_require_iri(value, field_name)` helper.
- **Find-tool pagination.** The `find_*` tools return a single ranked page. If callers
  need second-page results, expose `page` / `offset` parameters.
- **Async HTTP.** Only if latency becomes a real concern. Right now it doesn't.

## CI (also not yet)

No CI is wired up. Worth adding eventually:

- GitHub Actions workflow running `uv run pytest` + `uv run pyright` on every push.
- A nightly cron running `uv run pytest -m live` to catch BioPortal shape drift early.
- Both before publishing to PyPI, if that's ever wanted.

## Out of scope (do not add)

- **Anything that interprets natural language inside a tool.** See DESIGN.md Principle 2.
- **Knowledge of downstream consumers.** Tool docstrings, Field descriptions, parameter
  names, and defaults must stay domain-agnostic. See DESIGN.md Principle 1.
- **Generic BioPortal browsing / hierarchy walking.** This server is scoped to identifier
  resolution. "List all children of class X" or "show me the ontology tree" don't belong
  here — BioPortal's web UI exists for that.
- **Support for non-BioPortal terminology servers.** If a use case calls for OLS or a
  private server, that's a separate MCP, not parameters added here. See DESIGN.md
  Principle 4.
- **Server-side authentication beyond the BioPortal API key.** Single-key, single-tenant.
  If multi-tenant auth ever becomes needed, the deployment story changes substantially.

## Decisions made along the way

Recorded here so they don't get relitigated:

- **Python, not Java**, for this MCP. Anthropic's Python MCP SDK is the most mature; HTTP
  passthrough work doesn't benefit from JVM-locality.
- **`uv` as the package manager**, not pip or Poetry. Single binary, lockfile, fast.
- **Sync `httpx`**, not async. Simpler; latency hasn't been an issue.
- **`respx` for HTTP mocking**, not unittest.mock patches. respx mocks at the transport
  layer (correct level) and gives readable request/response assertions.
- **Pydantic `BaseModel`** for outputs, not `TypedDict` or raw `dict`. The Field
  descriptions become part of the LLM-visible tool schema.
- **BSD-2-Clause license**, matching the conventions of the project hosting the repo.
- **One server, one repo.** Reusable across consumers, with its own release cadence.
- **`find_value_set` requires `vs_collection` explicitly.** No presumed default list, so
  the server doesn't take an opinion about which consumer's value-set collections matter.
