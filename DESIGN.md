# Design

The architectural principles behind this MCP. Read these before adding new tools, refactoring,
or extending the surface — they're the answers to questions that aren't obvious from the code.

## Principle 1: Domain-agnostic by construction

This server resolves BioPortal resources to typed tuples. It does not know what those
tuples will be used for. Tool docstrings, Pydantic Field descriptions, parameter names,
and default values must not reference any downstream consumer (specific metadata-model
libraries, export tools, form generators, etc.).

The reason is reusability and layering: a domain-aware MCP that wraps this one can
reference both itself and BioPortal cleanly; a BioPortal MCP that references one specific
consumer cannot. Couplings flow downstream, never upstream.

**What this rules out**:

- Docstrings like *"this returns the tuple needed by Foo's builder"*.
- Field descriptions like *"value as expected by Bar.create()"*.
- Hardcoded defaults derived from one consumer's conventions (e.g. assuming a specific
  value-set-collection ontology because one downstream user prefers it).

**What replaces them**: descriptions of what the tool returns, in BioPortal's own terms.
If a consumer needs to map fields to a specific downstream API, that mapping belongs in
the consumer's own MCP / library / documentation, not here.

## Principle 2: The MCP is hands, the LLM is brain

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

## Principle 3: Tools are stateless

Every tool call is self-contained. No in-memory state carries between calls. No "current
ontology" or "active class" the user sets up first. The orchestrating LLM holds all
context; the MCP just resolves identifiers to tuples.

This rules out caching as a tool-level concern (we may add a transparent HTTP cache later,
but it's invisible to callers). It also rules out session-scoped configuration via tools.

## Principle 4: Compose, don't conflate

This server resolves BioPortal resources. It does not also build, validate, transform, or
export anything in a downstream domain. When a use case naturally combines BioPortal lookup
with downstream work (e.g. assembling a metadata artifact, generating an export file,
synthesizing a survey form), the right shape is **another MCP** that consumes this one's
output, not added parameters or tools here.

**Why**:
- Reusability: a domain-agnostic BioPortal MCP is useful to every consumer of BioPortal.
  A consumer-specific MCP that bundles BioPortal access with one domain's logic isn't.
- Single responsibility: this server has zero knowledge of any consumer's data model and
  must keep it that way.
- Composability: MCP clients (Claude, etc.) can connect to multiple MCP servers in parallel
  and orchestrate across them. Trust that mechanism rather than building an in-server
  super-tool.

Likewise, supporting **multiple terminology services** (OLS, a private server) belongs in
*another MCP per service*, not via parameters added to these tools.

## Principle 5: The `get_*` / `find_*` pairing

Each resource type (ontology / class / value set) gets two tools:

- `get_X(known_identifier)` — caller already knows the IRI / acronym; resolves to the
  canonical tuple. One HTTP call, deterministic output.
- `find_X(query, ...)` — free-text input; returns a ranked list of candidates. The
  orchestrating LLM picks one (using its own judgment) and may follow up with `get_X`
  to canonicalize.

The two patterns produce different return shapes (single tuple vs. ranked list) and
deserve separate tools rather than one overloaded tool with a `mode` flag.

## Principle 6: Strict client-side input validation

Every string argument runs through `_require_nonblank(value, field_name)` at the top of
the tool. The reasoning:

- Empty input shouldn't reach the network. A clear `ValueError` with the field name is
  more useful to the orchestrating LLM than a 301/404 from a malformed URL.
- The LLM can read the error message and adjust. Vague upstream errors confuse it.

When tools take an IRI (e.g. `get_class(class_iri, ...)`), a similar `_require_iri`
helper should be added — TODO for the first tool that takes an IRI it doesn't trust the
caller to have validated.

## Principle 7: Test layering

Every tool ships with:

1. **Mocked unit tests** using `respx`. The bulk of coverage. No network, deterministic,
   fast. Validate URL construction, response parsing, error mapping, input validation.
2. **One `@pytest.mark.live` test** per tool. Hits the real BioPortal API. Skipped by
   default (`addopts = "-m 'not live'"` in `pyproject.toml`). Purpose: guard against
   BioPortal silently changing their response shape.

The live tests intentionally don't replicate the unit-test coverage. They're a shape-drift
canary, not a comprehensive suite.

## Principle 8: Pydantic output models, not raw dicts

Every tool returns a `BaseModel` subclass with `Field(description=...)` annotations on each
field. FastMCP introspects these to generate the JSON Schema the orchestrating LLM sees.
Returning raw `dict` works but yields a much weaker tool description; the LLM has to guess
field semantics.

Output-model docstrings describe what each tuple identifies *in BioPortal's own terms*
(see Principle 1). They do not reference downstream consumers. The LLM combines that
schema with its own knowledge to chain across MCPs.

## Principle 9: HTTP-layer resilience without server-layer complexity

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
