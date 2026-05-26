"""
Tests for bioportal_term_mcp.server.

Tested in three layers:

1. Pure helper unit tests (no network, no env).
2. Tool tests with mocked HTTP responses via respx. These are the bulk; they assert
   that the tool constructs the right URL, parses BioPortal's response shape correctly,
   and surfaces errors cleanly. Fast, deterministic, run on every commit.
3. Optional live tests against the real BioPortal API, gated behind the `live` marker.
   Skipped by default. Run on-demand with `uv run pytest -m live`.
"""

from __future__ import annotations

import os

import httpx
import pytest
import respx
from httpx import Response

from bioportal_term_mcp.server import (
    ClassSearchHit,
    ClassTuple,
    OntologyTuple,
    ValueSetTuple,
    _extract_acronym_from_ontology_link,
    _first_label_string,
    _rank_ontology_match,
    _require_nonblank,
    find_class,
    find_ontology,
    find_value_set,
    get_class,
    get_ontology,
    get_value_set,
    ping,
)


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------


class TestRequireNonblank:
    def test_strips_surrounding_whitespace(self):
        assert _require_nonblank("  DOID  ", "acronym") == "DOID"

    def test_passes_through_already_clean_value(self):
        assert _require_nonblank("DOID", "acronym") == "DOID"

    @pytest.mark.parametrize("bad", ["", "   ", "\t", "\n"])
    def test_rejects_empty_and_whitespace_only(self, bad: str):
        with pytest.raises(ValueError, match="must be a non-empty string"):
            _require_nonblank(bad, "acronym")

    def test_field_name_appears_in_error_message(self):
        with pytest.raises(ValueError, match="my_field must be a non-empty"):
            _require_nonblank("", "my_field")


# ---------------------------------------------------------------------------
# ping
# ---------------------------------------------------------------------------


class TestPing:
    def test_echoes_the_input_message(self):
        assert ping("hello") == "pong: hello"


# ---------------------------------------------------------------------------
# get_ontology — mocked HTTP
#
# Each test sets BIOPORTAL_API_KEY via monkeypatch (no real key needed) and mocks
# the BioPortal endpoint with respx so the tool never reaches the network.
# ---------------------------------------------------------------------------


@pytest.fixture
def api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sets a fake BIOPORTAL_API_KEY for the duration of one test."""
    monkeypatch.setenv("BIOPORTAL_API_KEY", "test-fake-key")


class TestGetOntologyHappyPath:
    @respx.mock
    def test_returns_canonical_tuple(self, api_key: None):
        respx.get("https://data.bioontology.org/ontologies/DOID").mock(
            return_value=Response(
                200,
                json={
                    "@id": "https://data.bioontology.org/ontologies/DOID",
                    "acronym": "DOID",
                    "name": "Human Disease Ontology",
                },
            )
        )

        result = get_ontology("DOID")

        assert isinstance(result, OntologyTuple)
        assert result.acronym == "DOID"
        assert result.name == "Human Disease Ontology"
        assert result.ontology_iri == "https://data.bioontology.org/ontologies/DOID"

    @respx.mock
    def test_request_carries_authorization_header(self, api_key: None):
        route = respx.get("https://data.bioontology.org/ontologies/DOID").mock(
            return_value=Response(
                200,
                json={
                    "@id": "https://data.bioontology.org/ontologies/DOID",
                    "acronym": "DOID",
                    "name": "Human Disease Ontology",
                },
            )
        )

        get_ontology("DOID")

        # respx records the actual request; confirm we sent the right header shape.
        assert route.called
        sent_request = route.calls.last.request
        assert sent_request.headers["authorization"] == "apikey token=test-fake-key"

    @respx.mock
    def test_strips_whitespace_before_using_acronym_in_url(self, api_key: None):
        # Whitespace-padded input must be cleaned client-side; otherwise the URL would
        # contain encoded spaces and BioPortal would 404.
        route = respx.get("https://data.bioontology.org/ontologies/DOID").mock(
            return_value=Response(
                200,
                json={
                    "@id": "https://data.bioontology.org/ontologies/DOID",
                    "acronym": "DOID",
                    "name": "Human Disease Ontology",
                },
            )
        )

        result = get_ontology("  DOID  ")

        assert route.called
        assert result.acronym == "DOID"


class TestGetOntologyValidation:
    @pytest.mark.parametrize("bad", ["", "   ", "\t"])
    def test_rejects_empty_acronym_before_any_network_call(
        self, api_key: None, bad: str
    ):
        # No respx routes mocked — if we reached the network, the test would error
        # with a respx "no route" exception, surfacing the bug.
        with pytest.raises(ValueError, match="acronym must be a non-empty"):
            get_ontology(bad)


class TestGetOntologyErrors:
    @respx.mock
    def test_404_surfaces_as_http_status_error(self, api_key: None):
        respx.get("https://data.bioontology.org/ontologies/NOTAREALONT").mock(
            return_value=Response(404, json={"error": "not found"})
        )

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            get_ontology("NOTAREALONT")

        assert exc_info.value.response.status_code == 404

    @respx.mock
    def test_5xx_surfaces_as_http_status_error(self, api_key: None):
        respx.get("https://data.bioontology.org/ontologies/DOID").mock(
            return_value=Response(503)
        )

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            get_ontology("DOID")

        assert exc_info.value.response.status_code == 503

    def test_missing_api_key_raises_runtime_error(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Force the env var to be unset for this test, regardless of the host shell.
        monkeypatch.delenv("BIOPORTAL_API_KEY", raising=False)

        with pytest.raises(RuntimeError, match="BIOPORTAL_API_KEY"):
            get_ontology("DOID")

    def test_blank_api_key_raises_runtime_error(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # An env var set to whitespace shouldn't sneak through as if it were a real key.
        monkeypatch.setenv("BIOPORTAL_API_KEY", "   ")

        with pytest.raises(RuntimeError, match="BIOPORTAL_API_KEY"):
            get_ontology("DOID")


# ---------------------------------------------------------------------------
# _first_label_string helper
# ---------------------------------------------------------------------------


class TestFirstLabelString:
    def test_returns_string_value_as_is(self):
        assert _first_label_string("disease") == "disease"

    def test_returns_first_item_of_list(self):
        assert _first_label_string(["disease", "illness"]) == "disease"

    def test_skips_empty_strings_in_list(self):
        assert _first_label_string(["", "  ", "real value"]) == "real value"

    @pytest.mark.parametrize("empty", [None, "", "  ", [], [""], ["  "]])
    def test_returns_none_for_empty_or_missing(self, empty):
        assert _first_label_string(empty) is None

    def test_returns_none_for_unexpected_types(self):
        assert _first_label_string(42) is None
        assert _first_label_string({"label": "x"}) is None


# ---------------------------------------------------------------------------
# get_class — mocked HTTP
# ---------------------------------------------------------------------------


# Test fixture: a realistic DOID class IRI + the encoded form used in the URL path.
DOID_DISEASE_IRI = "http://purl.obolibrary.org/obo/DOID_4"
DOID_DISEASE_IRI_ENCODED = (
    "http%3A%2F%2Fpurl.obolibrary.org%2Fobo%2FDOID_4"
)


class TestGetClassHappyPath:
    @respx.mock
    def test_returns_canonical_tuple(self, api_key: None):
        respx.get(
            f"https://data.bioontology.org/ontologies/DOID/classes/{DOID_DISEASE_IRI_ENCODED}"
        ).mock(
            return_value=Response(
                200,
                json={
                    "@id": DOID_DISEASE_IRI,
                    "prefLabel": "disease",
                    "label": ["disease"],
                },
            )
        )
        respx.get("https://data.bioontology.org/ontologies/DOID").mock(
            return_value=Response(
                200,
                json={
                    "@id": "https://data.bioontology.org/ontologies/DOID",
                    "acronym": "DOID",
                    "name": "Human Disease Ontology",
                },
            )
        )

        result = get_class(DOID_DISEASE_IRI, "DOID")

        assert isinstance(result, ClassTuple)
        assert result.class_iri == DOID_DISEASE_IRI
        assert result.pref_label == "disease"
        assert result.label == "disease"
        assert result.ontology_acronym == "DOID"
        assert result.ontology_name == "Human Disease Ontology"

    @respx.mock
    def test_url_encodes_class_iri_correctly(self, api_key: None):
        # The IRI contains `:` and `/` which must be percent-encoded in the URL path,
        # otherwise BioPortal interprets the slashes as path delimiters and 404s.
        class_route = respx.get(
            f"https://data.bioontology.org/ontologies/DOID/classes/{DOID_DISEASE_IRI_ENCODED}"
        ).mock(
            return_value=Response(
                200,
                json={"@id": DOID_DISEASE_IRI, "prefLabel": "disease"},
            )
        )
        respx.get("https://data.bioontology.org/ontologies/DOID").mock(
            return_value=Response(
                200, json={"@id": "...", "acronym": "DOID", "name": "Human Disease Ontology"}
            )
        )

        get_class(DOID_DISEASE_IRI, "DOID")

        assert class_route.called

    @respx.mock
    def test_label_falls_back_to_pref_label_when_absent(self, api_key: None):
        # BioPortal sometimes omits rdfs:label entirely. The tool must still return a
        # usable `label` field so consumers don't end up with None.
        respx.get(
            f"https://data.bioontology.org/ontologies/DOID/classes/{DOID_DISEASE_IRI_ENCODED}"
        ).mock(
            return_value=Response(
                200,
                json={"@id": DOID_DISEASE_IRI, "prefLabel": "disease"},  # no "label" key
            )
        )
        respx.get("https://data.bioontology.org/ontologies/DOID").mock(
            return_value=Response(
                200, json={"@id": "...", "acronym": "DOID", "name": "Human Disease Ontology"}
            )
        )

        result = get_class(DOID_DISEASE_IRI, "DOID")

        assert result.pref_label == "disease"
        assert result.label == "disease"  # fell back to prefLabel

    @respx.mock
    def test_label_as_string_is_handled(self, api_key: None):
        # Some BioPortal responses return `label` as a single string rather than a list.
        respx.get(
            f"https://data.bioontology.org/ontologies/DOID/classes/{DOID_DISEASE_IRI_ENCODED}"
        ).mock(
            return_value=Response(
                200,
                json={
                    "@id": DOID_DISEASE_IRI,
                    "prefLabel": "disease",
                    "label": "disease (string-form)",
                },
            )
        )
        respx.get("https://data.bioontology.org/ontologies/DOID").mock(
            return_value=Response(
                200, json={"@id": "...", "acronym": "DOID", "name": "Human Disease Ontology"}
            )
        )

        result = get_class(DOID_DISEASE_IRI, "DOID")

        assert result.label == "disease (string-form)"


class TestGetClassValidation:
    @pytest.mark.parametrize("bad", ["", "   ", "\t"])
    def test_rejects_empty_class_iri(self, api_key: None, bad: str):
        with pytest.raises(ValueError, match="class_iri must be a non-empty"):
            get_class(bad, "DOID")

    @pytest.mark.parametrize("bad", ["", "   ", "\t"])
    def test_rejects_empty_ontology_acronym(self, api_key: None, bad: str):
        with pytest.raises(ValueError, match="ontology_acronym must be a non-empty"):
            get_class(DOID_DISEASE_IRI, bad)


class TestGetClassErrors:
    @respx.mock
    def test_class_404_surfaces_as_http_status_error(self, api_key: None):
        respx.get(
            f"https://data.bioontology.org/ontologies/DOID/classes/{DOID_DISEASE_IRI_ENCODED}"
        ).mock(return_value=Response(404, json={"error": "not found"}))

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            get_class(DOID_DISEASE_IRI, "DOID")

        assert exc_info.value.response.status_code == 404

    @respx.mock
    def test_ontology_404_surfaces_as_http_status_error(self, api_key: None):
        # Class call succeeds, ontology call fails — exercises the second-call path.
        respx.get(
            f"https://data.bioontology.org/ontologies/DOID/classes/{DOID_DISEASE_IRI_ENCODED}"
        ).mock(
            return_value=Response(200, json={"@id": DOID_DISEASE_IRI, "prefLabel": "disease"})
        )
        respx.get("https://data.bioontology.org/ontologies/DOID").mock(
            return_value=Response(404, json={"error": "not found"})
        )

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            get_class(DOID_DISEASE_IRI, "DOID")

        assert exc_info.value.response.status_code == 404


# ---------------------------------------------------------------------------
# _extract_acronym_from_ontology_link helper
# ---------------------------------------------------------------------------


class TestExtractAcronymFromOntologyLink:
    def test_extracts_acronym_from_standard_url(self):
        assert (
            _extract_acronym_from_ontology_link(
                "https://data.bioontology.org/ontologies/DOID"
            )
            == "DOID"
        )

    def test_strips_trailing_slash(self):
        assert (
            _extract_acronym_from_ontology_link(
                "https://data.bioontology.org/ontologies/NCIT/"
            )
            == "NCIT"
        )

    def test_returns_empty_for_empty_input(self):
        assert _extract_acronym_from_ontology_link("") == ""


# ---------------------------------------------------------------------------
# _rank_ontology_match helper
# ---------------------------------------------------------------------------


class TestRankOntologyMatch:
    def test_exact_acronym_match_ranks_first(self):
        exact = OntologyTuple(acronym="DOID", name="Human Disease Ontology", ontology_iri="x")
        prefix = OntologyTuple(acronym="DOIDX", name="other", ontology_iri="y")
        substr = OntologyTuple(acronym="OBO", name="contains doid somewhere", ontology_iri="z")

        ranked = sorted([substr, prefix, exact], key=lambda t: _rank_ontology_match(t, "doid"))

        assert ranked[0] is exact
        assert ranked[1] is prefix
        assert ranked[2] is substr

    def test_name_prefix_beats_substring(self):
        name_prefix = OntologyTuple(acronym="X", name="Disease Ontology", ontology_iri="a")
        substr = OntologyTuple(acronym="Y", name="other disease thing", ontology_iri="b")

        ranked = sorted(
            [substr, name_prefix], key=lambda t: _rank_ontology_match(t, "disease")
        )

        assert ranked[0] is name_prefix
        assert ranked[1] is substr

    def test_alphabetical_within_band(self):
        # Two equal-band matches sort alphabetically by acronym for determinism.
        zulu = OntologyTuple(acronym="ZULU", name="Zulu Ontology", ontology_iri="z")
        alpha = OntologyTuple(acronym="ALPHA", name="Alpha Ontology", ontology_iri="a")

        ranked = sorted([zulu, alpha], key=lambda t: _rank_ontology_match(t, "ontology"))

        # Both are band-3 substring matches; ALPHA comes first alphabetically.
        assert ranked[0] is alpha
        assert ranked[1] is zulu


# ---------------------------------------------------------------------------
# find_ontology — mocked HTTP
# ---------------------------------------------------------------------------


def _ontology_catalog(ontologies: list[dict]) -> list[dict]:
    """Builds a minimal /ontologies response payload."""
    return ontologies


# A small synthetic ontology catalog for exercising matching and ranking logic
# without coupling to BioPortal's actual catalog size.
_FAKE_CATALOG = [
    {
        "@id": "https://data.bioontology.org/ontologies/DOID",
        "acronym": "DOID",
        "name": "Human Disease Ontology",
    },
    {
        "@id": "https://data.bioontology.org/ontologies/NCIT",
        "acronym": "NCIT",
        "name": "National Cancer Institute Thesaurus",
    },
    {
        "@id": "https://data.bioontology.org/ontologies/MONDO",
        "acronym": "MONDO",
        "name": "Mondo Disease Ontology",
    },
    {
        "@id": "https://data.bioontology.org/ontologies/HRAVS",
        "acronym": "HRAVS",
        "name": "HRA Value Set",
    },
]


class TestFindOntologyHappyPath:
    @respx.mock
    def test_exact_acronym_match_ranks_first(self, api_key: None):
        respx.get("https://data.bioontology.org/ontologies").mock(
            return_value=Response(200, json=_FAKE_CATALOG)
        )

        results = find_ontology("DOID")

        assert len(results) >= 1
        assert results[0].acronym == "DOID"
        assert results[0].name == "Human Disease Ontology"
        assert results[0].ontology_iri == "https://data.bioontology.org/ontologies/DOID"

    @respx.mock
    def test_case_insensitive_acronym_match(self, api_key: None):
        respx.get("https://data.bioontology.org/ontologies").mock(
            return_value=Response(200, json=_FAKE_CATALOG)
        )

        results = find_ontology("doid")

        assert results[0].acronym == "DOID"

    @respx.mock
    def test_substring_in_name_matches(self, api_key: None):
        respx.get("https://data.bioontology.org/ontologies").mock(
            return_value=Response(200, json=_FAKE_CATALOG)
        )

        results = find_ontology("disease")

        # DOID and MONDO both have "Disease" in their names.
        acronyms = [r.acronym for r in results]
        assert "DOID" in acronyms
        assert "MONDO" in acronyms

    @respx.mock
    def test_no_match_returns_empty_list(self, api_key: None):
        respx.get("https://data.bioontology.org/ontologies").mock(
            return_value=Response(200, json=_FAKE_CATALOG)
        )

        results = find_ontology("xyzabc-no-match")

        assert results == []

    @respx.mock
    def test_max_results_caps_returned_list(self, api_key: None):
        respx.get("https://data.bioontology.org/ontologies").mock(
            return_value=Response(200, json=_FAKE_CATALOG)
        )

        # All four catalog entries contain "o" — request only the top 2.
        results = find_ontology("o", max_results=2)

        assert len(results) == 2

    @respx.mock
    def test_max_results_capped_at_50(self, api_key: None):
        # Even if the caller asks for 10000, we never return more than 50.
        # (Easier to verify with a larger synthetic catalog; here just confirm
        # the cap doesn't blow up with a small catalog.)
        big_catalog = [
            {
                "@id": f"https://data.bioontology.org/ontologies/ONT{i}",
                "acronym": f"ONT{i}",
                "name": f"Ontology {i}",
            }
            for i in range(100)
        ]
        respx.get("https://data.bioontology.org/ontologies").mock(
            return_value=Response(200, json=big_catalog)
        )

        results = find_ontology("ont", max_results=10000)

        assert len(results) == 50

    @respx.mock
    def test_skips_entries_with_no_acronym_or_name(self, api_key: None):
        # Defensive: if BioPortal ever returns a malformed entry with neither field,
        # we skip it rather than crash.
        catalog_with_junk = _FAKE_CATALOG + [{"@id": "junk", "acronym": "", "name": ""}]
        respx.get("https://data.bioontology.org/ontologies").mock(
            return_value=Response(200, json=catalog_with_junk)
        )

        # No assertion needed beyond "doesn't crash"; query something that wouldn't match
        # the junk entry anyway.
        results = find_ontology("disease")

        assert all(r.acronym for r in results)


class TestFindOntologyValidation:
    @pytest.mark.parametrize("bad", ["", "   ", "\t"])
    def test_rejects_empty_query(self, api_key: None, bad: str):
        with pytest.raises(ValueError, match="query must be a non-empty"):
            find_ontology(bad)


class TestFindOntologyErrors:
    @respx.mock
    def test_5xx_from_catalog_endpoint_surfaces(self, api_key: None):
        respx.get("https://data.bioontology.org/ontologies").mock(
            return_value=Response(503)
        )

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            find_ontology("disease")

        assert exc_info.value.response.status_code == 503


# ---------------------------------------------------------------------------
# find_class — mocked HTTP
# ---------------------------------------------------------------------------


def _search_response_with_hits(hits: list[dict]) -> dict:
    """Builds a minimal BioPortal /search response payload around a list of hit dicts."""
    return {
        "page": 1,
        "pageCount": 1,
        "totalCount": len(hits),
        "links": {},
        "prevPage": None,
        "nextPage": None,
        "collection": hits,
    }


class TestFindClassHappyPath:
    @respx.mock
    def test_returns_list_of_hits_with_inlined_ontology_metadata(self, api_key: None):
        respx.get("https://data.bioontology.org/search").mock(
            return_value=Response(
                200,
                json=_search_response_with_hits(
                    [
                        {
                            "@id": "http://purl.obolibrary.org/obo/DOID_4",
                            "prefLabel": "disease",
                            "label": ["disease"],
                            "ontology": {
                                "@id": "https://data.bioontology.org/ontologies/DOID",
                                "acronym": "DOID",
                                "name": "Human Disease Ontology",
                            },
                        },
                        {
                            "@id": "http://purl.obolibrary.org/obo/MONDO_0700096",
                            "prefLabel": "human disease",
                            "ontology": {
                                "@id": "https://data.bioontology.org/ontologies/MONDO",
                                "acronym": "MONDO",
                                "name": "Mondo Disease Ontology",
                            },
                        },
                    ]
                ),
            )
        )

        results = find_class("disease")

        assert len(results) == 2
        assert all(isinstance(hit, ClassSearchHit) for hit in results)
        assert results[0].class_iri == "http://purl.obolibrary.org/obo/DOID_4"
        assert results[0].pref_label == "disease"
        assert results[0].ontology_acronym == "DOID"
        assert results[0].ontology_name == "Human Disease Ontology"
        assert results[1].ontology_acronym == "MONDO"

    @respx.mock
    def test_falls_back_to_link_extraction_when_ontology_metadata_absent(
        self, api_key: None
    ):
        # BioPortal sometimes omits the inline `ontology` object and provides only the link.
        respx.get("https://data.bioontology.org/search").mock(
            return_value=Response(
                200,
                json=_search_response_with_hits(
                    [
                        {
                            "@id": "http://purl.obolibrary.org/obo/DOID_4",
                            "prefLabel": "disease",
                            "links": {
                                "ontology": "https://data.bioontology.org/ontologies/DOID"
                            },
                        }
                    ]
                ),
            )
        )

        results = find_class("disease")

        assert len(results) == 1
        assert results[0].ontology_acronym == "DOID"
        assert results[0].ontology_name is None  # not inlined; LLM can fetch separately

    @respx.mock
    def test_scopes_to_ontology_when_acronym_provided(self, api_key: None):
        route = respx.get("https://data.bioontology.org/search").mock(
            return_value=Response(200, json=_search_response_with_hits([]))
        )

        find_class("disease", ontology_acronym="DOID")

        # The `ontologies` query param should have been set to "DOID".
        sent_url = str(route.calls.last.request.url)
        assert "ontologies=DOID" in sent_url

    @respx.mock
    def test_omits_ontology_scope_when_acronym_is_none(self, api_key: None):
        route = respx.get("https://data.bioontology.org/search").mock(
            return_value=Response(200, json=_search_response_with_hits([]))
        )

        find_class("disease")

        sent_url = str(route.calls.last.request.url)
        assert "ontologies=" not in sent_url

    @respx.mock
    def test_max_results_sets_pagesize_query_param(self, api_key: None):
        route = respx.get("https://data.bioontology.org/search").mock(
            return_value=Response(200, json=_search_response_with_hits([]))
        )

        find_class("disease", max_results=10)

        sent_url = str(route.calls.last.request.url)
        assert "pagesize=10" in sent_url

    @respx.mock
    def test_max_results_caps_at_50(self, api_key: None):
        # Defensive: even if the caller asks for 10000, we send at most 50 to BioPortal.
        route = respx.get("https://data.bioontology.org/search").mock(
            return_value=Response(200, json=_search_response_with_hits([]))
        )

        find_class("disease", max_results=10000)

        sent_url = str(route.calls.last.request.url)
        assert "pagesize=50" in sent_url

    @respx.mock
    def test_max_results_floors_at_1(self, api_key: None):
        # Defensive: negative or zero results should clamp to 1, not produce a bad URL.
        route = respx.get("https://data.bioontology.org/search").mock(
            return_value=Response(200, json=_search_response_with_hits([]))
        )

        find_class("disease", max_results=0)

        sent_url = str(route.calls.last.request.url)
        assert "pagesize=1" in sent_url

    @respx.mock
    def test_empty_search_results_return_empty_list(self, api_key: None):
        # BioPortal returns a well-formed empty collection for no-match queries.
        # Empty list is the correct shape — not an error.
        respx.get("https://data.bioontology.org/search").mock(
            return_value=Response(200, json=_search_response_with_hits([]))
        )

        results = find_class("xyzabc-no-match")

        assert results == []


class TestFindClassValidation:
    @pytest.mark.parametrize("bad", ["", "   ", "\t"])
    def test_rejects_empty_query(self, api_key: None, bad: str):
        with pytest.raises(ValueError, match="query must be a non-empty"):
            find_class(bad)

    @pytest.mark.parametrize("bad", ["", "   ", "\t"])
    def test_rejects_blank_ontology_acronym_when_provided(self, api_key: None, bad: str):
        # `ontology_acronym=None` means "search everywhere" (valid). An empty *string* on
        # the other hand is a caller bug and should be rejected.
        with pytest.raises(ValueError, match="ontology_acronym must be a non-empty"):
            find_class("disease", ontology_acronym=bad)


class TestFindClassErrors:
    @respx.mock
    def test_5xx_from_search_endpoint_surfaces(self, api_key: None):
        respx.get("https://data.bioontology.org/search").mock(
            return_value=Response(503)
        )

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            find_class("disease")

        assert exc_info.value.response.status_code == 503


# ---------------------------------------------------------------------------
# find_value_set — mocked HTTP
# ---------------------------------------------------------------------------


class TestFindValueSetHappyPath:
    @respx.mock
    def test_returns_value_set_hits(self, api_key: None):
        respx.get("https://data.bioontology.org/search").mock(
            return_value=Response(
                200,
                json=_search_response_with_hits(
                    [
                        {
                            "@id": "https://purl.humanatlas.io/vocab/hravs#HRAVS_1000161",
                            "prefLabel": "Area unit",
                            "ontology": {
                                "@id": "https://data.bioontology.org/ontologies/HRAVS",
                                "acronym": "HRAVS",
                                "name": "HRA Value Set",
                            },
                        }
                    ]
                ),
            )
        )

        results = find_value_set("area unit", vs_collection="HRAVS")

        assert len(results) == 1
        assert isinstance(results[0], ValueSetTuple)
        assert results[0].value_set_iri == "https://purl.humanatlas.io/vocab/hravs#HRAVS_1000161"
        assert results[0].vs_collection == "HRAVS"
        assert results[0].name == "Area unit"
        # /search responses don't include child counts; always None for search hits.
        assert results[0].num_terms is None

    @respx.mock
    def test_vs_collection_scopes_the_search(self, api_key: None):
        route = respx.get("https://data.bioontology.org/search").mock(
            return_value=Response(200, json=_search_response_with_hits([]))
        )

        find_value_set("area unit", vs_collection="HRAVS")

        sent_url = str(route.calls.last.request.url)
        assert "ontologies=HRAVS" in sent_url

    @respx.mock
    def test_max_results_sets_pagesize(self, api_key: None):
        route = respx.get("https://data.bioontology.org/search").mock(
            return_value=Response(200, json=_search_response_with_hits([]))
        )

        find_value_set("area unit", vs_collection="HRAVS", max_results=5)

        sent_url = str(route.calls.last.request.url)
        assert "pagesize=5" in sent_url

    @respx.mock
    def test_max_results_capped_at_50(self, api_key: None):
        route = respx.get("https://data.bioontology.org/search").mock(
            return_value=Response(200, json=_search_response_with_hits([]))
        )

        find_value_set("area unit", vs_collection="HRAVS", max_results=10000)

        sent_url = str(route.calls.last.request.url)
        assert "pagesize=50" in sent_url

    @respx.mock
    def test_empty_results_return_empty_list(self, api_key: None):
        respx.get("https://data.bioontology.org/search").mock(
            return_value=Response(200, json=_search_response_with_hits([]))
        )

        results = find_value_set("xyzabc-no-match", vs_collection="HRAVS")

        assert results == []

    @respx.mock
    def test_falls_back_to_link_extraction_when_ontology_metadata_absent(
        self, api_key: None
    ):
        respx.get("https://data.bioontology.org/search").mock(
            return_value=Response(
                200,
                json=_search_response_with_hits(
                    [
                        {
                            "@id": "https://purl.humanatlas.io/vocab/hravs#HRAVS_1000161",
                            "prefLabel": "Area unit",
                            "links": {
                                "ontology": "https://data.bioontology.org/ontologies/HRAVS"
                            },
                        }
                    ]
                ),
            )
        )

        results = find_value_set("area unit", vs_collection="HRAVS")

        assert len(results) == 1
        assert results[0].vs_collection == "HRAVS"


class TestFindValueSetValidation:
    @pytest.mark.parametrize("bad", ["", "   ", "\t"])
    def test_rejects_empty_query(self, api_key: None, bad: str):
        with pytest.raises(ValueError, match="query must be a non-empty"):
            find_value_set(bad, vs_collection="HRAVS")

    @pytest.mark.parametrize("bad", ["", "   ", "\t"])
    def test_rejects_empty_vs_collection(self, api_key: None, bad: str):
        # vs_collection is required; an empty string is a bug.
        with pytest.raises(ValueError, match="vs_collection must be a non-empty"):
            find_value_set("area unit", vs_collection=bad)


class TestFindValueSetErrors:
    @respx.mock
    def test_5xx_surfaces_as_http_status_error(self, api_key: None):
        respx.get("https://data.bioontology.org/search").mock(
            return_value=Response(503)
        )

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            find_value_set("area unit", vs_collection="HRAVS")

        assert exc_info.value.response.status_code == 503


# ---------------------------------------------------------------------------
# get_value_set — mocked HTTP
# ---------------------------------------------------------------------------


HRAVS_AREA_UNIT_IRI = "https://purl.humanatlas.io/vocab/hravs#HRAVS_1000161"
HRAVS_AREA_UNIT_IRI_ENCODED = (
    "https%3A%2F%2Fpurl.humanatlas.io%2Fvocab%2Fhravs%23HRAVS_1000161"
)


class TestGetValueSetHappyPath:
    @respx.mock
    def test_returns_canonical_tuple(self, api_key: None):
        respx.get(
            f"https://data.bioontology.org/ontologies/HRAVS/classes/{HRAVS_AREA_UNIT_IRI_ENCODED}"
        ).mock(
            return_value=Response(
                200,
                json={
                    "@id": HRAVS_AREA_UNIT_IRI,
                    "prefLabel": "Area unit",
                    "numChildren": 40,
                },
            )
        )

        result = get_value_set(HRAVS_AREA_UNIT_IRI, "HRAVS")

        assert isinstance(result, ValueSetTuple)
        assert result.value_set_iri == HRAVS_AREA_UNIT_IRI
        assert result.vs_collection == "HRAVS"
        assert result.name == "Area unit"
        assert result.num_terms == 40

    @respx.mock
    def test_url_encodes_value_set_iri_correctly(self, api_key: None):
        # IRI contains `:`, `/`, and `#` which must all be percent-encoded in the URL path.
        route = respx.get(
            f"https://data.bioontology.org/ontologies/HRAVS/classes/{HRAVS_AREA_UNIT_IRI_ENCODED}"
        ).mock(
            return_value=Response(
                200, json={"@id": HRAVS_AREA_UNIT_IRI, "prefLabel": "Area unit"}
            )
        )

        get_value_set(HRAVS_AREA_UNIT_IRI, "HRAVS")

        assert route.called

    @respx.mock
    def test_num_terms_is_none_when_absent(self, api_key: None):
        # BioPortal often omits a count from the class endpoint. The tool must still
        # return a usable tuple; num_terms is documented as best-effort / Optional.
        respx.get(
            f"https://data.bioontology.org/ontologies/HRAVS/classes/{HRAVS_AREA_UNIT_IRI_ENCODED}"
        ).mock(
            return_value=Response(
                200,
                json={"@id": HRAVS_AREA_UNIT_IRI, "prefLabel": "Area unit"},
            )
        )

        result = get_value_set(HRAVS_AREA_UNIT_IRI, "HRAVS")

        assert result.num_terms is None
        assert result.name == "Area unit"

    @respx.mock
    def test_num_terms_is_none_when_non_integer(self, api_key: None):
        # Defensive: if BioPortal ever returns a non-int for numChildren (string, null,
        # list), the tool falls back to None rather than crashing.
        respx.get(
            f"https://data.bioontology.org/ontologies/HRAVS/classes/{HRAVS_AREA_UNIT_IRI_ENCODED}"
        ).mock(
            return_value=Response(
                200,
                json={
                    "@id": HRAVS_AREA_UNIT_IRI,
                    "prefLabel": "Area unit",
                    "numChildren": "forty",
                },
            )
        )

        result = get_value_set(HRAVS_AREA_UNIT_IRI, "HRAVS")

        assert result.num_terms is None


class TestGetValueSetValidation:
    @pytest.mark.parametrize("bad", ["", "   ", "\t"])
    def test_rejects_empty_value_set_iri(self, api_key: None, bad: str):
        with pytest.raises(ValueError, match="value_set_iri must be a non-empty"):
            get_value_set(bad, "HRAVS")

    @pytest.mark.parametrize("bad", ["", "   ", "\t"])
    def test_rejects_empty_vs_collection(self, api_key: None, bad: str):
        with pytest.raises(ValueError, match="vs_collection must be a non-empty"):
            get_value_set(HRAVS_AREA_UNIT_IRI, bad)


class TestGetValueSetErrors:
    @respx.mock
    def test_404_surfaces_as_http_status_error(self, api_key: None):
        respx.get(
            f"https://data.bioontology.org/ontologies/HRAVS/classes/{HRAVS_AREA_UNIT_IRI_ENCODED}"
        ).mock(return_value=Response(404, json={"error": "not found"}))

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            get_value_set(HRAVS_AREA_UNIT_IRI, "HRAVS")

        assert exc_info.value.response.status_code == 404


# ---------------------------------------------------------------------------
# Live tests — opt-in
#
# Run with:    uv run pytest -m live
# Requires:    BIOPORTAL_API_KEY set in the environment.
# Purpose:     guard against BioPortal silently changing their response shape.
# ---------------------------------------------------------------------------


@pytest.mark.live
class TestGetOntologyLive:
    def test_doid_resolves(self):
        if not os.environ.get("BIOPORTAL_API_KEY"):
            pytest.skip("BIOPORTAL_API_KEY not set; skipping live test.")

        result = get_ontology("DOID")

        assert result.acronym == "DOID"
        assert "Disease" in result.name
        assert result.ontology_iri.startswith("https://data.bioontology.org/ontologies/")


@pytest.mark.live
class TestGetClassLive:
    def test_doid_disease_class_resolves(self):
        if not os.environ.get("BIOPORTAL_API_KEY"):
            pytest.skip("BIOPORTAL_API_KEY not set; skipping live test.")

        result = get_class("http://purl.obolibrary.org/obo/DOID_4", "DOID")

        assert result.class_iri == "http://purl.obolibrary.org/obo/DOID_4"
        assert result.ontology_acronym == "DOID"
        assert "Disease" in result.ontology_name
        # Both label and prefLabel should be populated and non-empty.
        assert result.pref_label
        assert result.label


@pytest.mark.live
class TestGetValueSetLive:
    def test_hravs_area_unit_resolves(self):
        if not os.environ.get("BIOPORTAL_API_KEY"):
            pytest.skip("BIOPORTAL_API_KEY not set; skipping live test.")

        result = get_value_set(
            "https://purl.humanatlas.io/vocab/hravs#HRAVS_1000161", "HRAVS"
        )

        assert result.value_set_iri == "https://purl.humanatlas.io/vocab/hravs#HRAVS_1000161"
        assert result.vs_collection == "HRAVS"
        assert result.name  # whatever BioPortal returns, it should be non-empty


@pytest.mark.live
class TestFindOntologyLive:
    def test_finds_doid_by_substring(self):
        if not os.environ.get("BIOPORTAL_API_KEY"):
            pytest.skip("BIOPORTAL_API_KEY not set; skipping live test.")

        results = find_ontology("human disease", max_results=10)

        assert len(results) > 0
        # DOID should be in the top 10 results for "human disease".
        acronyms = [r.acronym for r in results]
        assert "DOID" in acronyms

    def test_exact_acronym_ranks_first(self):
        if not os.environ.get("BIOPORTAL_API_KEY"):
            pytest.skip("BIOPORTAL_API_KEY not set; skipping live test.")

        results = find_ontology("NCIT", max_results=10)

        assert len(results) > 0
        # NCIT itself should be the first result for an exact-acronym query.
        assert results[0].acronym == "NCIT"


@pytest.mark.live
class TestFindValueSetLive:
    def test_search_hravs_for_area_unit_finds_known_value_set(self):
        if not os.environ.get("BIOPORTAL_API_KEY"):
            pytest.skip("BIOPORTAL_API_KEY not set; skipping live test.")

        results = find_value_set("area unit", vs_collection="HRAVS", max_results=10)

        assert len(results) > 0
        # All hits should be in HRAVS (the scope we requested).
        assert all(hit.vs_collection == "HRAVS" for hit in results)
        # The canonical HRAVS_1000161 (Area unit) should be among the top hits for this query.
        iris = [hit.value_set_iri for hit in results]
        assert any("HRAVS_1000161" in iri for iri in iris)


@pytest.mark.live
class TestFindClassLive:
    def test_search_for_disease_in_doid_finds_doid_4(self):
        if not os.environ.get("BIOPORTAL_API_KEY"):
            pytest.skip("BIOPORTAL_API_KEY not set; skipping live test.")

        results = find_class("disease", ontology_acronym="DOID", max_results=10)

        # Sanity checks: should get some results, all should carry DOID acronym.
        assert len(results) > 0
        assert all(hit.ontology_acronym == "DOID" for hit in results)
        # The literal "disease" class (DOID_4) should be among the top results for this query.
        iris = [hit.class_iri for hit in results]
        assert "http://purl.obolibrary.org/obo/DOID_4" in iris

    def test_unscoped_search_returns_hits_from_multiple_ontologies(self):
        if not os.environ.get("BIOPORTAL_API_KEY"):
            pytest.skip("BIOPORTAL_API_KEY not set; skipping live test.")

        results = find_class("melanoma", max_results=20)

        assert len(results) > 0
        # Without scope, we expect hits from at least two ontologies in the top 20.
        acronyms = {hit.ontology_acronym for hit in results if hit.ontology_acronym}
        assert len(acronyms) >= 2
