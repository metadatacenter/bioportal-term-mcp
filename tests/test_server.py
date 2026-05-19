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
    OntologyTuple,
    _require_nonblank,
    get_ontology,
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
