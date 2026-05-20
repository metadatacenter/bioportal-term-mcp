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
    ClassTuple,
    OntologyTuple,
    ValueSetTuple,
    _first_label_string,
    _require_nonblank,
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
        # usable `label` field so the CEDAR builder doesn't end up with None.
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
        # return a usable tuple; CEDAR has a 3-arg overload for the no-count case.
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
