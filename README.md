# bioportal-term-mcp

[BioPortal](https://bioportal.bioontology.org/) is the largest open repository
of biomedical (and increasingly cross-domain) ontologies — over 1,000
ontologies and value sets covering diseases, anatomy, phenotypes, chemicals,
publication types, units of measure, and more. The point of working with an
ontology rather than free text is the **IRI**: a stable identifier that says
"this thing is *this* concept in *this* ontology", with a canonical human
label attached, so the data is checkable, queryable, and interoperable rather
than a string somebody has to interpret.

LLMs are surprisingly bad at IRIs. They'll happily invent
`http://purl.obolibrary.org/obo/DOID_10923` and have it be wrong by one
digit, or pick a deprecated parent class, or confuse acronyms across
ontologies. The fix is to make BioPortal lookup a first-class capability the
LLM can actually call — not paste-from-memory.

This is a [Model Context Protocol](https://modelcontextprotocol.io/) server
that exposes BioPortal as six MCP tools. Two access modes (free-text
**search** and exact **lookup-by-identifier**), three resource types
(ontologies, classes, value sets), plus a `ping`. Each tool returns a
canonical typed tuple — `(IRI, acronym, name, ...)` — designed to be
threaded into whatever the LLM does next: building a metadata schema,
populating an instance, annotating a dataset, generating a form. The MCP
itself does none of that downstream work. Tools designed for specific
domains should run as **separate MCP servers** that consume this one's
output; couplings flow downstream, not upstream (see
[DESIGN.md Principle 1](./DESIGN.md)).

## Example workflow

A typical session — natural-language prompts the user gives the LLM, which
the LLM translates into MCP tool calls. The pattern is **discovery →
canonicalization → hand-off**: find candidates with `find_*`, pin a specific
one with `get_*` (or just take the top `find_*` hit when it's already a full
tuple), and pass the canonical record on to whatever comes next.

*Find an ontology covering human diseases.*

```json
[
  { "acronym": "DOID",  "name": "Human Disease Ontology",   "ontology_iri": "https://data.bioontology.org/ontologies/DOID" },
  { "acronym": "MONDO", "name": "Mondo Disease Ontology",   "ontology_iri": "https://data.bioontology.org/ontologies/MONDO" },
  { "acronym": "HPO",   "name": "Human Phenotype Ontology", "ontology_iri": "https://data.bioontology.org/ontologies/HPO" }
]
```

The LLM calls `find_ontology("human disease")` and presents the ranked
candidates. The user (or the LLM, depending on the context) picks `DOID`.

*Find sickle cell anemia in DOID.*

```json
[
  {
    "class_iri": "http://purl.obolibrary.org/obo/DOID_10923",
    "pref_label": "sickle cell anemia",
    "label": "sickle cell anemia",
    "ontology_acronym": "DOID",
    "ontology_name": "Human Disease Ontology"
  }
]
```

The LLM calls `find_class("sickle cell anemia", "DOID")`. `DOID_10923`
comes back as the top hit with the canonical IRI, both labels, and the
ontology metadata already populated — no separate `get_class` round-trip
needed for this case.

*Confirm what `DOID_10923` is.*

```json
{
  "class_iri": "http://purl.obolibrary.org/obo/DOID_10923",
  "pref_label": "sickle cell anemia",
  "label": "sickle cell anemia",
  "ontology_acronym": "DOID",
  "ontology_name": "Human Disease Ontology"
}
```

When the LLM already has an IRI from a prior conversation, a saved
annotation, or an external system, it calls `get_class` to verify and fetch
the canonical record. This is the "I trust this IRI but I want the current
labels" path.

The canonical tuple is now ready to hand to whatever downstream MCP or tool
the orchestrating LLM is also driving — a metadata-template builder, an
annotation pipeline, a form generator. Each tool call is stateless; the
orchestrating LLM holds the context between calls.

## Tools

Six tools across three resource types and two access modes, plus a diagnostic `ping`:

|              | known identifier                              | free-text search                              |
|---           |---                                            |---                                            |
| **ontology** | `get_ontology(acronym)`                       | `find_ontology(query)`                        |
| **class**    | `get_class(class_iri, ontology_acronym)`      | `find_class(query, ontology_acronym?)`        |
| **value set**| `get_value_set(value_set_iri, vs_collection)` | `find_value_set(query, vs_collection)`        |

Each tool is detailed below with signature, motivation, and a concrete example.

---

### `get_ontology(acronym)`

```python
get_ontology(acronym: str) -> OntologyTuple
```

Resolves a known BioPortal ontology acronym to its canonical `(acronym, name, ontology_iri)`
triple. One HTTP call to `GET /ontologies/{acronym}`.

**When to use.** The caller knows the acronym (e.g. `DOID`, `NCIT`, `HRAVS`) and needs the
canonical metadata for it. For free-text discovery, use `find_ontology` instead.

**Example**

Input:

```json
{ "acronym": "DOID" }
```

Output:

```json
{
  "acronym": "DOID",
  "name": "Human Disease Ontology",
  "ontology_iri": "https://data.bioontology.org/ontologies/DOID"
}
```

Errors: empty acronym → `ValueError`; unknown acronym → 404 surfaced as a tool error.

---

### `find_ontology(query, max_results=20)`

```python
find_ontology(query: str, max_results: int = 20) -> list[OntologyTuple]
```

Free-text search over BioPortal's full ontology catalog, returning a ranked list of
matching ontologies. Each hit has the same shape as `get_ontology` — no follow-up call is
needed to canonicalize.

**Ranking** (best match first):

1. Exact acronym match (case-insensitive)
2. Acronym prefix match
3. Name prefix match
4. Substring match in acronym or name

Ties within a band are broken alphabetically by acronym for determinism.

**When to use.** The caller knows part of the ontology's name or acronym but not the
exact value (`"human disease"`, `"cancer"`, lowercase `"ncit"`). `max_results` is capped
at 50 client-side.

**Implementation note.** BioPortal has no server-side text-search endpoint for ontologies,
so the tool fetches the full catalog and filters / ranks client-side. One HTTP call to
`GET /ontologies` per invocation.

**Example**

Input:

```json
{ "query": "human disease", "max_results": 3 }
```

Output:

```json
[
  { "acronym": "DOID",  "name": "Human Disease Ontology",     "ontology_iri": "https://data.bioontology.org/ontologies/DOID" },
  { "acronym": "MONDO", "name": "Mondo Disease Ontology",     "ontology_iri": "https://data.bioontology.org/ontologies/MONDO" },
  { "acronym": "HPO",   "name": "Human Phenotype Ontology",   "ontology_iri": "https://data.bioontology.org/ontologies/HPO" }
]
```

---

### `get_class(class_iri, ontology_acronym)`

```python
get_class(class_iri: str, ontology_acronym: str) -> ClassTuple
```

Resolves a known class IRI within a known ontology to a canonical 5-tuple identifying the
class: `(class_iri, pref_label, label, ontology_acronym, ontology_name)`.

`pref_label` is `skos:prefLabel`; `label` is `rdfs:label` (or falls back to `pref_label`
if BioPortal doesn't return one).

**When to use.** The caller has the class IRI in hand (e.g. from a prior `find_class` or
from external metadata) and needs the full canonical identification.

**Implementation note.** Two HTTP calls happen: one to
`GET /ontologies/{acronym}/classes/{url-encoded-iri}` for the class data, one to
`GET /ontologies/{acronym}` for the ontology's display name.

**Example**

Input:

```json
{
  "class_iri": "http://purl.obolibrary.org/obo/DOID_4",
  "ontology_acronym": "DOID"
}
```

Output:

```json
{
  "class_iri": "http://purl.obolibrary.org/obo/DOID_4",
  "pref_label": "disease",
  "label": "disease",
  "ontology_acronym": "DOID",
  "ontology_name": "Human Disease Ontology"
}
```

---

### `find_class(query, ontology_acronym?, max_results=20)`

```python
find_class(query: str, ontology_acronym: str | None = None, max_results: int = 20) -> list[ClassSearchHit]
```

Free-text search for ontology classes via BioPortal's `/search` endpoint. Returns a list
of `ClassSearchHit` records ordered by BioPortal's relevance score.

`ClassSearchHit` is *lighter* than `ClassTuple` — `ontology_name` may be `None` because
BioPortal's search response doesn't always inline it. For the full canonical 5-tuple,
follow up with `get_class(hit.class_iri, hit.ontology_acronym)`.

**When to use.** The caller knows the term name but not the IRI. Pass `ontology_acronym`
to scope the search to a single ontology; omit it to search all of BioPortal.

`max_results` is capped at 50 client-side.

**Example (scoped)**

Input:

```json
{ "query": "disease", "ontology_acronym": "DOID", "max_results": 3 }
```

Output:

```json
[
  {
    "class_iri": "http://purl.obolibrary.org/obo/DOID_4",
    "pref_label": "disease",
    "label": "disease",
    "ontology_acronym": "DOID",
    "ontology_name": "Human Disease Ontology"
  },
  {
    "class_iri": "http://purl.obolibrary.org/obo/DOID_7",
    "pref_label": "disease of anatomical entity",
    "label": "disease of anatomical entity",
    "ontology_acronym": "DOID",
    "ontology_name": "Human Disease Ontology"
  }
]
```

**Example (unscoped)**

Input:

```json
{ "query": "melanoma", "max_results": 5 }
```

Output: hits drawn from multiple ontologies (DOID, NCIT, MONDO, MESH, etc.), ranked by
BioPortal's relevance score across the full corpus.

---

### `get_value_set(value_set_iri, vs_collection)`

```python
get_value_set(value_set_iri: str, vs_collection: str) -> ValueSetTuple
```

Resolves a known value-set IRI within a named value-set collection to its canonical
triple: `(value_set_iri, vs_collection, name)`.

Value sets in BioPortal are classes within special "value-set collection" ontologies
(e.g. `CEDARVS`, `HRAVS`); the collection acronym behaves like an ontology acronym in
BioPortal's URL structure.

**When to use.** The caller has the value-set IRI and knows which collection contains it.

**Example**

Input:

```json
{
  "value_set_iri": "https://purl.humanatlas.io/vocab/hravs#HRAVS_1000161",
  "vs_collection": "HRAVS"
}
```

Output:

```json
{
  "value_set_iri": "https://purl.humanatlas.io/vocab/hravs#HRAVS_1000161",
  "vs_collection": "HRAVS",
  "name": "Area unit"
}
```

---

### `find_value_set(query, vs_collection, max_results=20)`

```python
find_value_set(query: str, vs_collection: str, max_results: int = 20) -> list[ValueSetTuple]
```

Free-text search for value sets within a named collection, returning a ranked list of
candidates.

**`vs_collection` is required** — the caller must name the collection to search (e.g.
`CEDARVS`, `HRAVS`). The server intentionally does not presume a default, because BioPortal
hosts value-set collections for multiple downstream communities and choosing one would
couple this tool to a specific consumer.

**When to use.** The caller knows part of the value set's name but not its IRI, and knows
which value-set collection is relevant for their domain.

`max_results` is capped at 50 client-side.

**Example**

Input:

```json
{ "query": "area unit", "vs_collection": "HRAVS", "max_results": 3 }
```

Output:

```json
[
  {
    "value_set_iri": "https://purl.humanatlas.io/vocab/hravs#HRAVS_1000161",
    "vs_collection": "HRAVS",
    "name": "Area unit"
  }
]
```

---

### `ping(message)`

```python
ping(message: str) -> str
```

Diagnostic round-trip. Echoes `pong: <message>` back. Useful for verifying the
MCP server is reachable from a client, with no BioPortal API call involved.

**Example**

| Input | Output |
|---|---|
| `message="hello"` | `"pong: hello"` |

---

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
