"""
MCP server for resolving BioPortal ontologies, classes, and value sets into canonical
(IRI, acronym, name, ...) tuples.

Six tools across three resource types and two access modes:

                          known-identifier            free-text search
  ontology                get_ontology                find_ontology
  class                   get_class                   find_class
  value set               get_value_set               find_value_set

The server has no knowledge of any downstream consumer. Each tool returns a typed tuple
identifying the requested resource; consumers map those tuples into their own domain
models.
"""

from __future__ import annotations

import os
import urllib.parse
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

BIOPORTAL_BASE_URL = "https://data.bioontology.org"
HTTP_TIMEOUT_SECONDS = 30.0


def _api_key() -> str:
    """Reads the BioPortal API key from BIOPORTAL_API_KEY. Raises if absent."""
    key = os.environ.get("BIOPORTAL_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "BIOPORTAL_API_KEY env var is not set. "
            "Get a free key at https://bioportal.bioontology.org/account "
            "and export BIOPORTAL_API_KEY=<your-key> before launching the MCP."
        )
    return key


# A JSON object as BioPortal returns it. Its values are read through the guards below, which
# check each value's shape before use.
JsonObject = Mapping[str, object]


def _bioportal_get(path: str, params: dict[str, str] | None = None) -> object:
    """Authenticated GET against the BioPortal REST API, returning the decoded JSON body.

    Follows 3xx redirects so a benign restructure on the BioPortal side doesn't surface
    as a tool error. Raises httpx.HTTPStatusError on non-2xx final responses; FastMCP
    turns those into structured tool errors the LLM can read.
    """
    headers = {"Authorization": f"apikey token={_api_key()}", "Accept": "application/json"}
    url = f"{BIOPORTAL_BASE_URL}{path}"
    with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=True) as client:
        response = client.get(url, headers=headers, params=params or {})
        response.raise_for_status()
        return response.json()


def _bioportal_get_object(path: str, params: dict[str, str] | None = None) -> JsonObject:
    """GET a BioPortal endpoint that answers with a JSON object, as most do."""
    payload = _bioportal_get(path, params)
    if not isinstance(payload, dict):
        raise ValueError(
            f"BioPortal answered {path} with a JSON {type(payload).__name__}; expected an object."
        )
    return payload


def _bioportal_get_array(path: str, params: dict[str, str] | None = None) -> list[object]:
    """GET a BioPortal endpoint that answers with a JSON array, such as `/ontologies`."""
    payload = _bioportal_get(path, params)
    if not isinstance(payload, list):
        raise ValueError(
            f"BioPortal answered {path} with a JSON {type(payload).__name__}; expected an array."
        )
    return payload


def _optional_string(obj: JsonObject, key: str) -> str | None:
    """The value at `key` when it is a non-empty string, otherwise None."""
    value = obj.get(key)
    return value if isinstance(value, str) and value else None


def _required_string(obj: JsonObject, key: str, what: str) -> str:
    """The string at `key`. Raises when it is absent or not a string, naming `what` was read."""
    value = obj.get(key)
    if not isinstance(value, str):
        raise ValueError(f"BioPortal's {what} carries no string '{key}'; got {value!r}.")
    return value


def _nested_object(obj: JsonObject, key: str) -> JsonObject:
    """The object at `key`, or an empty object when it is absent or not an object."""
    value = obj.get(key)
    return value if isinstance(value, dict) else {}


def _objects(items: list[object]) -> list[JsonObject]:
    """The items that are JSON objects."""
    return [item for item in items if isinstance(item, dict)]


def _require_nonblank(value: str, field_name: str) -> str:
    """Strips whitespace and rejects empty values with a clear, LLM-readable message."""
    stripped = value.strip() if value else ""
    if not stripped:
        raise ValueError(f"{field_name} must be a non-empty string; got {value!r}.")
    return stripped


SERVER_NAME = "bioportal-term-mcp"

mcp = FastMCP(SERVER_NAME)


def _server_version() -> str:
    """Reads the installed package version, falling back to "unknown" when the package metadata
    is unavailable (e.g. running from a source tree that was never installed)."""
    try:
        return version(SERVER_NAME)
    except PackageNotFoundError:
        return "unknown"


SERVER_VERSION = _server_version()


# ---------------------------------------------------------------------------
# Tool: ping (diagnostic, no API call)
# ---------------------------------------------------------------------------

@mcp.tool()
def ping(message: str) -> str:
    """Echoes the message back, with the server name and version appended. Used to verify the MCP
    server is reachable and to report which build is running."""
    return f"pong: {message} ({SERVER_NAME} {SERVER_VERSION})"


# ---------------------------------------------------------------------------
# Tool: get_ontology
#
# Maps a known BioPortal acronym (e.g. "DOID") to its canonical (acronym, name,
# ontology_iri) triple.
# ---------------------------------------------------------------------------

class OntologyTuple(BaseModel):
    """Canonical identification of a BioPortal ontology."""

    acronym: str = Field(description="Ontology acronym, e.g. 'DOID'.")
    name: str = Field(description="Human-readable ontology name, e.g. 'Human Disease Ontology'.")
    ontology_iri: str = Field(description="Canonical IRI for the ontology in BioPortal.")


@mcp.tool()
def get_ontology(acronym: str) -> OntologyTuple:
    """Resolves a BioPortal ontology acronym to its canonical (acronym, name, ontology_iri) triple.

    Use this when the caller already knows the acronym (e.g. 'DOID', 'NCIT', 'HRAVS').
    For free-text lookup (e.g. 'Human Disease Ontology' without the acronym), use
    `find_ontology` instead.

    Raises an error if the acronym is empty, unknown, or BioPortal is unreachable.
    """
    acronym = _require_nonblank(acronym, "acronym")
    payload = _bioportal_get_object(f"/ontologies/{acronym}")
    return OntologyTuple(
        acronym=_required_string(payload, "acronym", "ontology"),
        name=_required_string(payload, "name", "ontology"),
        ontology_iri=_required_string(payload, "@id", "ontology"),
    )


# ---------------------------------------------------------------------------
# Tool: find_ontology
#
# Free-text search over BioPortal's ontology catalog. Unlike find_class,
# BioPortal has no server-side text-search endpoint for ontologies — the
# /ontologies endpoint returns the full catalog as a flat list, which we
# rank client-side. The cost is one HTTP call per invocation; trivially
# cacheable later if it becomes a hotspot.
# ---------------------------------------------------------------------------


def _rank_ontology_match(tuple_: OntologyTuple, query_lower: str) -> tuple[int, str]:
    """Sort key: more-specific matches sort first.

    Priorities (lowest tuple key wins):
      0: exact acronym match (case-insensitive)
      1: acronym prefix match
      2: name prefix match
      3: substring match in either field
    Within each band, sort alphabetically by acronym for determinism.
    """
    acro = tuple_.acronym.lower()
    name = tuple_.name.lower()
    if acro == query_lower:
        band = 0
    elif acro.startswith(query_lower):
        band = 1
    elif name.startswith(query_lower):
        band = 2
    else:
        band = 3
    return (band, tuple_.acronym.lower())


@mcp.tool()
def find_ontology(query: str, max_results: int = 20) -> list[OntologyTuple]:
    """Free-text search for BioPortal ontologies, returning a ranked list of candidates.

    Use this when the caller knows part of the ontology's name or acronym but not the
    exact value (e.g. 'human disease', 'cancer', 'NCIT'). For known-acronym lookup, use
    `get_ontology(acronym)` directly.

    Each hit carries the same (acronym, name, ontology_iri) triple `get_ontology` would
    return — no follow-up call is needed.

    Matching is case-insensitive substring over both the acronym and the human-readable
    name. Ranking prefers exact acronym matches, then acronym/name prefix matches, then
    substring hits. `max_results` is capped at 50 client-side.

    Implementation note: BioPortal's `/ontologies` endpoint returns the full catalog;
    filtering and ranking happen client-side. One HTTP call per invocation.
    """
    query = _require_nonblank(query, "query")
    query_lower = query.lower()
    capped_max = max(1, min(max_results, _MAX_SEARCH_RESULTS))

    payload = _bioportal_get_array("/ontologies")

    candidates: list[OntologyTuple] = []
    for entry in _objects(payload):
        acronym = _optional_string(entry, "acronym") or ""
        name = _optional_string(entry, "name") or ""
        if not acronym and not name:
            continue
        if query_lower in acronym.lower() or query_lower in name.lower():
            candidates.append(
                OntologyTuple(
                    acronym=acronym,
                    name=name,
                    ontology_iri=_optional_string(entry, "@id") or "",
                )
            )

    candidates.sort(key=lambda t: _rank_ontology_match(t, query_lower))
    return candidates[:capped_max]


# ---------------------------------------------------------------------------
# Tool: get_class
#
# Maps a known class IRI within a known ontology to a canonical identification
# tuple (class_iri, pref_label, label, ontology_acronym, ontology_name).
# ---------------------------------------------------------------------------


class ClassTuple(BaseModel):
    """Canonical identification of a class within a BioPortal ontology."""

    class_iri: str = Field(description="Canonical IRI for the class.")
    pref_label: str = Field(description="skos:prefLabel for the class.")
    label: str = Field(
        description="rdfs:label for the class, or the prefLabel if no separate label exists."
    )
    ontology_acronym: str = Field(description="Acronym of the containing ontology, e.g. 'DOID'.")
    ontology_name: str = Field(
        description="Human-readable name of the containing ontology, e.g. 'Human Disease Ontology'."
    )


def _first_label_string(raw: object) -> str | None:
    """Coerces BioPortal's polymorphic label field into a single string, or None.

    BioPortal returns rdfs:label either as a single string, a list of strings, or
    absent. This normalizes all three cases.
    """
    if isinstance(raw, str) and raw.strip():
        return raw
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and item.strip():
                return item
    return None


@mcp.tool()
def get_class(class_iri: str, ontology_acronym: str) -> ClassTuple:
    """Resolves a class IRI within a BioPortal ontology to its canonical 5-tuple.

    Returns (class_iri, pref_label, label, ontology_acronym, ontology_name). Use this
    when the caller already knows the class IRI (e.g. 'http://purl.obolibrary.org/obo/DOID_4').
    For free-text lookup (e.g. 'Disease in DOID'), use `find_class` instead.

    Two HTTP calls happen: one to the class endpoint, one to the ontology endpoint to
    fetch the ontology's display name. Raises an error if either lookup fails.
    """
    class_iri = _require_nonblank(class_iri, "class_iri")
    ontology_acronym = _require_nonblank(ontology_acronym, "ontology_acronym")

    # BioPortal class endpoint: /ontologies/{acronym}/classes/{double-url-encoded-iri}.
    # `safe=''` forces encoding of `:` and `/` which are normally path delimiters.
    encoded_iri = urllib.parse.quote(class_iri, safe="")
    class_payload = _bioportal_get_object(f"/ontologies/{ontology_acronym}/classes/{encoded_iri}")
    ontology_payload = _bioportal_get_object(f"/ontologies/{ontology_acronym}")

    found_iri = _required_string(class_payload, "@id", "class")
    pref_label = _optional_string(class_payload, "prefLabel") or found_iri
    label = _first_label_string(class_payload.get("label")) or pref_label

    return ClassTuple(
        class_iri=found_iri,
        pref_label=pref_label,
        label=label,
        ontology_acronym=_required_string(ontology_payload, "acronym", "ontology"),
        ontology_name=_required_string(ontology_payload, "name", "ontology"),
    )


# ---------------------------------------------------------------------------
# Tool: find_class
#
# Free-text search for classes across BioPortal, optionally scoped to one
# ontology. Returns a ranked list of candidates rather than a single tuple;
# the orchestrating LLM picks the right one and may follow up with
# get_class() to canonicalize.
# ---------------------------------------------------------------------------


class ClassSearchHit(BaseModel):
    """One candidate result from a class search.

    Lighter than `ClassTuple` because search responses don't always inline the
    full ontology metadata. When `ontology_name` is None, the orchestrating LLM
    can call `get_ontology(ontology_acronym)` to fill it, or call `get_class`
    on the chosen hit to get the full canonical 5-tuple.
    """

    class_iri: str = Field(description="Canonical IRI for the class.")
    pref_label: str = Field(description="skos:prefLabel for the class.")
    label: str = Field(
        description="rdfs:label for the class, or the prefLabel if no separate label exists."
    )
    ontology_acronym: str = Field(description="Acronym of the containing ontology, e.g. 'DOID'.")
    ontology_name: str | None = Field(
        default=None,
        description=(
            "Human-readable ontology name if BioPortal's search inlined it; None otherwise. "
            "Call get_ontology(ontology_acronym) to fill in the None case."
        ),
    )


# Page-size cap. BioPortal's defaults are sane, but we cap client-side too so a
# misbehaving caller can't ask for 10,000 results and dump them into the LLM's context.
_MAX_SEARCH_RESULTS = 50


def _extract_acronym_from_ontology_link(link: str) -> str:
    """Extracts e.g. 'DOID' from 'https://data.bioontology.org/ontologies/DOID'.

    Used as a fallback when a BioPortal search hit doesn't inline the ontology
    metadata. Returns "" if the link doesn't end in an acronym segment.
    """
    if not link:
        return ""
    return link.rstrip("/").rsplit("/", 1)[-1]


def _search_hits(payload: JsonObject) -> list[JsonObject]:
    """The hits of a `/search` answer: the objects in its `collection` array."""
    collection = payload.get("collection")
    return _objects(collection) if isinstance(collection, list) else []


def _hit_ontology_acronym(entry: JsonObject) -> str:
    """The acronym of a search hit's ontology, from the inlined `ontology` object when it has
    one and from the `links.ontology` URL otherwise."""
    return _optional_string(_nested_object(entry, "ontology"), "acronym") or (
        _extract_acronym_from_ontology_link(
            _optional_string(_nested_object(entry, "links"), "ontology") or ""
        )
    )


@mcp.tool()
def find_class(
    query: str,
    ontology_acronym: str | None = None,
    max_results: int = 20,
) -> list[ClassSearchHit]:
    """Free-text search for ontology classes, returning a ranked list of candidates.

    Use this when the caller knows the term name but not the IRI (e.g. 'disease',
    'melanoma'). Optionally scope to a single ontology by passing its acronym
    (e.g. 'DOID', 'NCIT'); without scope, BioPortal searches all ontologies and
    returns the highest-ranking matches across the full corpus.

    The returned list is ordered by BioPortal's relevance score. Each hit carries
    enough information to identify the class (IRI, label, source ontology). For
    the full canonical 5-tuple, follow up with `get_class(hit.class_iri, hit.ontology_acronym)`.

    `max_results` is capped at 50 client-side; values above that are silently
    truncated to avoid flooding the orchestrating LLM's context with low-ranked
    candidates.
    """
    query = _require_nonblank(query, "query")
    capped_max = max(1, min(max_results, _MAX_SEARCH_RESULTS))

    params: dict[str, str] = {"q": query, "pagesize": str(capped_max)}
    if ontology_acronym is not None:
        # Only validate when actually provided; absent (None) means "search all ontologies".
        params["ontologies"] = _require_nonblank(ontology_acronym, "ontology_acronym")

    payload = _bioportal_get_object("/search", params=params)

    hits: list[ClassSearchHit] = []
    for entry in _search_hits(payload):
        # BioPortal sometimes inlines ontology metadata as an `ontology` object;
        # sometimes only as a `links.ontology` URL. Handle both.
        ontology_meta = _nested_object(entry, "ontology")
        acronym = _hit_ontology_acronym(entry)

        class_iri = _required_string(entry, "@id", "search hit")
        pref_label = _optional_string(entry, "prefLabel") or class_iri
        label = _first_label_string(entry.get("label")) or pref_label

        hits.append(
            ClassSearchHit(
                class_iri=class_iri,
                pref_label=pref_label,
                label=label,
                ontology_acronym=acronym,
                ontology_name=_optional_string(ontology_meta, "name"),
            )
        )

    return hits


# ---------------------------------------------------------------------------
# Tool: get_value_set
#
# Value sets in BioPortal are classes within special "value-set collection"
# ontologies (e.g. CEDARVS, HRAVS). This tool maps a known value-set IRI
# within a known collection to its canonical identification tuple.
# ---------------------------------------------------------------------------


class ValueSetTuple(BaseModel):
    """Canonical identification of a value set within a BioPortal value-set collection."""

    value_set_iri: str = Field(description="Canonical IRI for the value set.")
    vs_collection: str = Field(
        description="Acronym of the value-set collection ontology, e.g. 'CEDARVS' or 'HRAVS'."
    )
    name: str = Field(description="Human-readable name of the value set (skos:prefLabel).")


@mcp.tool()
def get_value_set(value_set_iri: str, vs_collection: str) -> ValueSetTuple:
    """Resolves a value-set IRI within a BioPortal value-set collection to its canonical triple.

    Returns (value_set_iri, vs_collection, name). Value sets in BioPortal are classes
    within special "value-set collection" ontologies (CEDARVS, HRAVS, etc.); the
    collection acronym behaves like an ontology acronym in BioPortal's URL structure.

    Use this when the caller already knows the value-set IRI. For free-text lookup,
    use `find_value_set` instead.
    """
    value_set_iri = _require_nonblank(value_set_iri, "value_set_iri")
    vs_collection = _require_nonblank(vs_collection, "vs_collection")

    # Value sets are classes within the vs-collection ontology in BioPortal's URL space.
    encoded_iri = urllib.parse.quote(value_set_iri, safe="")
    payload = _bioportal_get_object(f"/ontologies/{vs_collection}/classes/{encoded_iri}")

    found_iri = _required_string(payload, "@id", "value set")
    name = _optional_string(payload, "prefLabel") or found_iri

    return ValueSetTuple(
        value_set_iri=found_iri,
        vs_collection=vs_collection,
        name=name,
    )


# ---------------------------------------------------------------------------
# Tool: find_value_set
#
# Free-text search for value sets within a specified value-set-collection
# ontology. The caller must name the collection (e.g. 'CEDARVS', 'HRAVS') —
# there is no presumed default, because BioPortal hosts value-set collections
# for multiple downstream communities and presuming one would couple this
# tool to a specific consumer.
# ---------------------------------------------------------------------------


@mcp.tool()
def find_value_set(
    query: str,
    vs_collection: str,
    max_results: int = 20,
) -> list[ValueSetTuple]:
    """Free-text search for value sets within a named collection, returning a ranked list.

    Use this when the caller knows part of the value set's name (e.g. 'area unit')
    but not its IRI. For known-IRI lookup, use `get_value_set` directly.

    `vs_collection` is required — pass the acronym of the value-set-collection ontology
    to search (e.g. 'CEDARVS', 'HRAVS'). The caller is expected to know which collection
    is relevant for their domain; this MCP intentionally does not presume one.

    Each hit carries (value_set_iri, vs_collection, name) — the same shape as
    `get_value_set`'s return.

    `max_results` is capped at 50 client-side.
    """
    query = _require_nonblank(query, "query")
    vs_collection = _require_nonblank(vs_collection, "vs_collection")
    capped_max = max(1, min(max_results, _MAX_SEARCH_RESULTS))

    params: dict[str, str] = {
        "q": query,
        "ontologies": vs_collection,
        "pagesize": str(capped_max),
    }
    payload = _bioportal_get_object("/search", params=params)

    hits: list[ValueSetTuple] = []
    for entry in _search_hits(payload):
        value_set_iri = _required_string(entry, "@id", "search hit")
        hits.append(
            ValueSetTuple(
                value_set_iri=value_set_iri,
                vs_collection=_hit_ontology_acronym(entry),
                name=_optional_string(entry, "prefLabel") or value_set_iri,
            )
        )

    return hits


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point referenced by the `bioportal-term-mcp` console script in pyproject.toml."""
    mcp.run()


if __name__ == "__main__":
    main()
