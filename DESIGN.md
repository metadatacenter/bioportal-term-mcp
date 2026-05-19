# Design

The architectural principles behind this MCP. Read these before adding new tools, refactoring,
or extending the surface — they're the answers to questions that aren't obvious from the code.

## Principle 1: The MCP is hands, the LLM is brain

This server does not interpret natural language. It exposes a small, opinionated set of
typed tools, each mapped to a single BioPortal API operation and returning a single
canonical tuple. Natural-language parsing ("the user said 'Disease in DOID', what do they
mean?") is the job of the *orchestrating* LLM (Claude / whatever client connects to this
MCP).

**What this rules out**: tools like `parse_natural_language_to_constraint(text)` or
`build_field_from_description(text)`. Those are LLM calls dressed up as tools, and they
duplicate what the orchestrating LLM is already doing. They also make the MCP non-deterministic
and hard to test.

**What this implies for new tools**: each one should be a thin wrapper over one BioPortal
endpoint (or at most two, if a single tool naturally bundles related lookups). If a tool's
implementation contains anything resembling "use this LLM to figure out X," it's wrong.

## Principle 2: Tools are stateless

Every tool call is self-contained. No in-memory state carries between calls. No "current
ontology" or "active class" the user sets up first. The orchestrating LLM holds all
context; the MCP just resolves identifiers to tuples.

This rules out caching as a tool-level concern (we may add a transparent HTTP cache later,
but it's invisible to callers). It also rules out session-scoped configuration via tools.

## Principle 3: Two MCPs, not one

This server is intentionally scoped to BioPortal-term resolution. A future companion
server, `cedar-artifact-mcp`, will expose the CEDAR artifact library's builders as tools.
The orchestrating LLM uses both: BioPortal to resolve names → IRIs, then artifact-mcp to
assemble those IRIs into CEDAR templates / fields / instances.

**Why split**:
- Reusability: a BioPortal MCP is useful beyond CEDAR; anyone resolving ontology terms
  benefits. Bundling them makes both harder to reuse.
- Single responsibility: this server has no idea what a CEDAR template is, and shouldn't.
- Composability: MCP encourages multi-server orchestration. Trust it.

## Principle 4: The `get_*` / `find_*` pairing

Each resource type (ontology / class / value set) gets two tools:

- `get_X(known_identifier)` — caller already knows the IRI / acronym; resolves to the
  canonical tuple. One HTTP call, deterministic output.
- `find_X(query, ...)` — free-text input; returns a ranked list of candidates. The
  orchestrating LLM picks one (using its own judgment) and may follow up with `get_X`
  to canonicalize.

The two patterns produce different return shapes (single tuple vs. ranked list) and
deserve separate tools rather than one overloaded tool with a `mode` flag.

## Principle 5: Strict client-side input validation

Every string argument runs through `_require_nonblank(value, field_name)` at the top of
the tool. The reasoning:

- Empty input shouldn't reach the network. A clear `ValueError` with the field name is
  more useful to the orchestrating LLM than a 301/404 from a malformed URL.
- The LLM can read the error message and adjust. Vague upstream errors confuse it.

When tools take an IRI (e.g. `get_class(class_iri, ...)`), a similar `_require_iri`
helper should be added — TODO for the first tool that takes an IRI it doesn't trust the
caller to have validated.

## Principle 6: Test layering

Every tool ships with:

1. **Mocked unit tests** using `respx`. The bulk of coverage. No network, deterministic,
   fast. Validate URL construction, response parsing, error mapping, input validation.
2. **One `@pytest.mark.live` test** per tool. Hits the real BioPortal API. Skipped by
   default (`addopts = "-m 'not live'"` in `pyproject.toml`). Purpose: guard against
   BioPortal silently changing their response shape.

The live tests intentionally don't replicate the unit-test coverage. They're a shape-drift
canary, not a comprehensive suite.

## Principle 7: Pydantic output models, not raw dicts

Every tool returns a `BaseModel` subclass with `Field(description=...)` annotations on each
field. FastMCP introspects these to generate the JSON Schema the orchestrating LLM sees.
Returning raw `dict` works but yields a much weaker tool description; the LLM has to guess
field semantics.

The output model's docstring should explicitly call out which CEDAR builder method's
arguments it fills, so the orchestrating LLM can chain correctly:

> "The fields needed by `withClassValueConstraint(uri, source, label, prefLabel, type)`
> and `withBranchValueConstraint(uri, source, acronym, name, maxDepth)`."

## Principle 8: HTTP-layer resilience without server-layer complexity

Network behavior happens in one place: `_bioportal_get`. Configured once:
- `follow_redirects=True` (BioPortal sometimes 301s on trailing-slash variants)
- `timeout=30s`
- API key header

Tools don't configure HTTP themselves. If a tool needs different network behavior, that's
a sign the abstraction needs adjusting, not that the tool should reach into httpx directly.

## What's *not* a principle (open to change)

- **Async vs. sync**: currently sync. If performance becomes a concern (multiple BioPortal
  calls per tool — e.g. `get_class` already makes two), switching `_bioportal_get` to async
  is a tractable change. Not blocking anything yet.
- **Result caching**: nothing is cached today. If BioPortal rate-limiting becomes a problem
  or the same ontologies are looked up repeatedly within a session, a simple TTL cache in
  `_bioportal_get` is the right place to add it. Stays invisible to tools.
- **Multiple terminology servers**: currently BioPortal-only. If we ever need to support
  e.g. OLS or a private terminology server, the right shape is *another MCP* (per Principle 3),
  not parameters added to these tools.
