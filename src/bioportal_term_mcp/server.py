"""
MCP server for resolving free-text descriptions of BioPortal ontologies, classes, and
value sets into the canonical (IRI, acronym, name, ...) tuples required by the CEDAR
artifact library's controlled-term-field builders.

Six tools planned, mapped to what the four CEDAR constraint builders need:

  withOntologyValueConstraint   <- get_ontology / find_ontology
  withBranchValueConstraint     <- find_class / get_class
  withClassValueConstraint      <- find_class / get_class
  withValueSetValueConstraint   <- find_value_set / get_value_set
"""

from __future__ import annotations

import os
import urllib.parse

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


def _bioportal_get(path: str, params: dict[str, str] | None = None) -> dict | list:
    """Authenticated GET against the BioPortal REST API.

    Most BioPortal endpoints return a JSON object; the `/ontologies` listing endpoint
    returns a JSON array, hence the union return type. Callers are responsible for
    knowing which shape to expect for the path they invoke.

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


def _require_nonblank(value: str, field_name: str) -> str:
    """Strips whitespace and rejects empty values with a clear, LLM-readable message."""
    stripped = value.strip() if value else ""
    if not stripped:
        raise ValueError(f"{field_name} must be a non-empty string; got {value!r}.")
    return stripped


mcp = FastMCP("bioportal-term-mcp")


# ---------------------------------------------------------------------------
# Tool: ping (diagnostic, no API call)
# ---------------------------------------------------------------------------

@mcp.tool()
def ping(message: str) -> str:
    """Echoes the message back. Used to verify the MCP server is reachable."""
    return f"pong: {message}"


# ---------------------------------------------------------------------------
# Tool: get_ontology
#
# Maps a known BioPortal acronym (e.g. "DOID") to the canonical 3-tuple the
# CEDAR library's `withOntologyValueConstraint(uri, acronym, name)` builder
# needs.
# ---------------------------------------------------------------------------

class OntologyTuple(BaseModel):
    """The 3-tuple expected by `ControlledTermField.builder().withOntologyValueConstraint(...)`."""

    acronym: str = Field(description="Ontology acronym, e.g. 'DOID'.")
    name: str = Field(description="Human-readable ontology name, e.g. 'Human Disease Ontology'.")
    ontology_iri: str = Field(description="Canonical IRI for the ontology in BioPortal.")


@mcp.tool()
def get_ontology(acronym: str) -> OntologyTuple:
    """Resolves a BioPortal ontology acronym to the canonical (acronym, name, ontology_iri) tuple.

    Use this when the caller already knows the acronym (e.g. 'DOID', 'NCIT', 'HRAVS').
    The returned tuple fills the three arguments of CEDAR's `withOntologyValueConstraint`.
    For free-text lookup (e.g. 'Human Disease Ontology' without the acronym), use
    `find_ontology` instead.

    Raises an error if the acronym is empty, unknown, or BioPortal is unreachable.
    """
    acronym = _require_nonblank(acronym, "acronym")
    payload = _bioportal_get(f"/ontologies/{acronym}")
    assert isinstance(payload, dict)
    return OntologyTuple(
        acronym=payload["acronym"],
        name=payload["name"],
        ontology_iri=payload["@id"],
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

    Each hit carries the same (acronym, name, ontology_iri) tuple that fills CEDAR's
    `withOntologyValueConstraint` — no follow-up call is needed.

    Matching is case-insensitive substring over both the acronym and the human-readable
    name. Ranking prefers exact acronym matches, then acronym/name prefix matches, then
    substring hits. `max_results` is capped at 50 client-side.

    Implementation note: BioPortal's `/ontologies` endpoint returns the full catalog;
    filtering and ranking happen client-side. One HTTP call per invocation.
    """
    query = _require_nonblank(query, "query")
    query_lower = query.lower()
    capped_max = max(1, min(max_results, _MAX_SEARCH_RESULTS))

    payload = _bioportal_get("/ontologies")
    assert isinstance(payload, list)

    candidates: list[OntologyTuple] = []
    for entry in payload:
        acronym = entry.get("acronym", "")
        name = entry.get("name", "")
        if not acronym and not name:
            continue
        if query_lower in acronym.lower() or query_lower in name.lower():
            candidates.append(
                OntologyTuple(
                    acronym=acronym,
                    name=name,
                    ontology_iri=entry.get("@id", ""),
                )
            )

    candidates.sort(key=lambda t: _rank_ontology_match(t, query_lower))
    return candidates[:capped_max]


# ---------------------------------------------------------------------------
# Tool: get_class
#
# Maps a known class IRI within a known ontology to the 5-tuple needed by
# both `withClassValueConstraint(uri, source, label, prefLabel, type)` and
# `withBranchValueConstraint(uri, source, acronym, name, maxDepth)`. The
# `type` (ValueType enum) and `maxDepth` arguments are caller-supplied and
# not derivable from BioPortal.
# ---------------------------------------------------------------------------


class ClassTuple(BaseModel):
    """The fields needed by `withClassValueConstraint` and `withBranchValueConstraint`.

    The CEDAR builders take additional caller-supplied arguments not present here:
    `ValueType` (for `withClassValueConstraint`) and `maxDepth` (for
    `withBranchValueConstraint`). Both are user-intent fields, not BioPortal data.
    """

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
    """Resolves a class IRI within a BioPortal ontology to the 5-tuple needed by CEDAR.

    The returned tuple fills (uri, source=ontology_name, label, prefLabel, ...) for
    `withClassValueConstraint`, and (uri, source=ontology_name, acronym, name=prefLabel, ...)
    for `withBranchValueConstraint`. Two arguments those builders accept are NOT in this
    tuple because they're user-intent fields:

      - `ValueType` enum (e.g. ONTOLOGY_CLASS) for class constraints
      - `maxDepth` integer for branch constraints

    Use this when the caller already knows the class IRI (e.g. 'http://purl.obolibrary.org/obo/DOID_4').
    For free-text lookup (e.g. 'Disease in DOID'), use `find_class` instead.

    Two HTTP calls happen: one to the class endpoint, one to the ontology endpoint to
    fetch the ontology's display name. Raises an error if either lookup fails.
    """
    class_iri = _require_nonblank(class_iri, "class_iri")
    ontology_acronym = _require_nonblank(ontology_acronym, "ontology_acronym")

    # BioPortal class endpoint: /ontologies/{acronym}/classes/{double-url-encoded-iri}.
    # `safe=''` forces encoding of `:` and `/` which are normally path delimiters.
    encoded_iri = urllib.parse.quote(class_iri, safe="")
    class_payload = _bioportal_get(f"/ontologies/{ontology_acronym}/classes/{encoded_iri}")
    ontology_payload = _bioportal_get(f"/ontologies/{ontology_acronym}")

    pref_label = class_payload.get("prefLabel") or class_payload["@id"]
    label = _first_label_string(class_payload.get("label")) or pref_label

    return ClassTuple(
        class_iri=class_payload["@id"],
        pref_label=pref_label,
        label=label,
        ontology_acronym=ontology_payload["acronym"],
        ontology_name=ontology_payload["name"],
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
    enough information to identify the class (IRI, label, source ontology); for
    the full canonical 5-tuple needed by CEDAR's `withClassValueConstraint`, follow
    up with `get_class(hit.class_iri, hit.ontology_acronym)`.

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

    payload = _bioportal_get("/search", params=params)

    hits: list[ClassSearchHit] = []
    for entry in payload.get("collection", []):
        # BioPortal sometimes inlines ontology metadata as an `ontology` object;
        # sometimes only as a `links.ontology` URL. Handle both.
        ontology_meta = entry.get("ontology") or {}
        acronym = ontology_meta.get("acronym") or _extract_acronym_from_ontology_link(
            entry.get("links", {}).get("ontology", "")
        )

        pref_label = entry.get("prefLabel") or entry.get("@id", "")
        label = _first_label_string(entry.get("label")) or pref_label

        hits.append(
            ClassSearchHit(
                class_iri=entry["@id"],
                pref_label=pref_label,
                label=label,
                ontology_acronym=acronym,
                ontology_name=ontology_meta.get("name"),
            )
        )

    return hits


# ---------------------------------------------------------------------------
# Tool: get_value_set
#
# Maps a known value-set IRI within a known value-set collection to the
# 4-tuple needed by CEDAR's `withValueSetValueConstraint(uri, vsCollection,
# name, numTerms?)`. Value sets in BioPortal are classes within special
# "value set collection" ontologies (e.g. CEDARVS, HRAVS).
# ---------------------------------------------------------------------------


class ValueSetTuple(BaseModel):
    """The fields needed by `withValueSetValueConstraint(uri, vsCollection, name, numTerms?)`.

    `num_terms` is best-effort: BioPortal's class endpoint doesn't always include a count,
    and paginating the descendants endpoint just to get a count is too expensive for a
    single tool call. Returns None when the count isn't available cheaply; callers can
    pass the 3-arg `withValueSetValueConstraint` overload in that case.
    """

    value_set_iri: str = Field(description="Canonical IRI for the value set.")
    vs_collection: str = Field(
        description="Acronym of the value-set collection ontology, e.g. 'CEDARVS' or 'HRAVS'."
    )
    name: str = Field(description="Human-readable name of the value set (skos:prefLabel).")
    num_terms: int | None = Field(
        default=None,
        description="Number of terms in the value set, if cheaply available. None otherwise.",
    )


@mcp.tool()
def get_value_set(value_set_iri: str, vs_collection: str) -> ValueSetTuple:
    """Resolves a value-set IRI within a BioPortal value-set collection to the 4-tuple needed by CEDAR.

    The returned tuple fills (uri, vsCollection, name, numTerms?) for
    `withValueSetValueConstraint`. Value sets in BioPortal are classes within special
    "value set collection" ontologies (CEDARVS, HRAVS, etc.); the collection acronym
    behaves like an ontology acronym in BioPortal's URL structure.

    Use this when the caller already knows the value-set IRI. For free-text lookup,
    use `find_value_set` instead.

    `num_terms` is returned as None unless BioPortal cheaply exposes the count in its
    class response. Callers who don't need the count can pass the 3-arg form of
    `withValueSetValueConstraint`.
    """
    value_set_iri = _require_nonblank(value_set_iri, "value_set_iri")
    vs_collection = _require_nonblank(vs_collection, "vs_collection")

    # Value sets are classes within the vs-collection ontology in BioPortal's URL space.
    encoded_iri = urllib.parse.quote(value_set_iri, safe="")
    payload = _bioportal_get(f"/ontologies/{vs_collection}/classes/{encoded_iri}")

    name = payload.get("prefLabel") or payload["@id"]
    # Some BioPortal responses expose a count under various keys. Be defensive: any
    # integer-typed field hinting at a count is acceptable; otherwise leave as None.
    raw_count = payload.get("numChildren")
    num_terms = raw_count if isinstance(raw_count, int) else None

    return ValueSetTuple(
        value_set_iri=payload["@id"],
        vs_collection=vs_collection,
        name=name,
        num_terms=num_terms,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point referenced by the `bioportal-term-mcp` console script in pyproject.toml."""
    mcp.run()


if __name__ == "__main__":
    main()
