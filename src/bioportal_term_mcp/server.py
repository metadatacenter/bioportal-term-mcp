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


def _bioportal_get(path: str, params: dict[str, str] | None = None) -> dict:
    """Authenticated GET against the BioPortal REST API.

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
    return OntologyTuple(
        acronym=payload["acronym"],
        name=payload["name"],
        ontology_iri=payload["@id"],
    )


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
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point referenced by the `bioportal-term-mcp` console script in pyproject.toml."""
    mcp.run()


if __name__ == "__main__":
    main()
