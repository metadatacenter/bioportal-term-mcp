"""
Records the BioPortal answers that the replay tests in test_server.py serve.

The script makes the tool calls the replay tests make, through the server's own code,
against the real BioPortal API. It captures every HTTP exchange those calls cause and
writes each response to tests/fixtures/bioportal/, one JSON file per distinct request.
A request that several calls make is stored once. Files that no longer correspond to a
request are removed.

Every answer is stored as BioPortal sent it, with one exception. The answer to
`GET /ontologies` lists every ontology in BioPortal and runs to several megabytes. The
script keeps every entry of it, but only the fields server.py reads from each entry:
`@id`, `acronym` and `name`. `find_ontology` therefore ranks the stored answer exactly as
it ranks the full one. The stored file says so in its `trimmed` field.

Run it from the repository root with a BioPortal API key in the environment:

    BIOPORTAL_API_KEY=<your-key> uv run python tests/record_bioportal_answers.py

The key never reaches a file. Requests carry it in the Authorization header, and the
script stores no headers. It also removes any `apikey` query parameter from the stored
URL and replaces any other occurrence of the key in the stored text with a placeholder.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
import urllib.parse
from collections.abc import Callable
from pathlib import Path

import httpx
import respx

from bioportal_term_mcp.server import (
    BIOPORTAL_BASE_URL,
    find_class,
    find_ontology,
    find_value_set,
    get_class,
    get_ontology,
    get_value_set,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "bioportal"
REDACTED = "REDACTED"

# The catalog answer and the fields find_ontology reads from each of its entries.
CATALOG_PATH = "/ontologies"
CATALOG_FIELDS = ("@id", "acronym", "name")
TRIMMED = "only the fields server.py reads from each entry"

# The tool calls the replay tests make, in the order the tests appear.
RECORDED_CALLS: list[Callable[[], object]] = [
    lambda: get_ontology("DOID"),
    lambda: get_class("http://purl.obolibrary.org/obo/DOID_4", "DOID"),
    lambda: get_value_set("https://purl.humanatlas.io/vocab/hravs#HRAVS_1000161", "HRAVS"),
    lambda: find_ontology("human disease", max_results=10),
    lambda: find_ontology("NCIT", max_results=10),
    lambda: find_value_set("area unit", vs_collection="HRAVS", max_results=10),
    lambda: find_class("disease", ontology_acronym="DOID", max_results=10),
    lambda: find_class("melanoma", max_results=20),
]


def fixture_name(url: httpx.URL) -> str:
    """Derives a readable file name from a request URL.

    Path segments come first, then the query parameters in sorted order. A segment that
    holds a whole IRI contributes only its local name, after the last `/` or `#`.
    """
    raw_path = url.raw_path.decode("ascii").split("?", 1)[0]
    parts = [
        re.split(r"[/#]", urllib.parse.unquote(segment))[-1]
        for segment in raw_path.strip("/").split("/")
    ]
    for key, value in sorted(url.params.multi_items()):
        parts += [key, value]
    return "-".join(re.sub(r"[^A-Za-z0-9_.]+", "_", part) for part in parts) + ".json"


def stored_response(path: str, status: int, body: object) -> dict[str, object]:
    """Builds the stored form of an answer.

    The catalog answer keeps every entry, each reduced to CATALOG_FIELDS. Any other
    answer, or a catalog answer that is not a list, is stored unchanged, so a change in
    its shape reaches the replay tests.
    """
    if path != CATALOG_PATH or not isinstance(body, list):
        return {"status": status, "body": body}
    entries = [
        {k: v for k, v in entry.items() if k in CATALOG_FIELDS}
        if isinstance(entry, dict)
        else entry
        for entry in body
    ]
    return {"status": status, "trimmed": TRIMMED, "body": entries}


def redact(text: str, key: str) -> str:
    """Replaces the key, in its plain and percent-encoded forms, with a placeholder."""
    for form in {key, urllib.parse.quote(key, safe="")}:
        text = text.replace(form, REDACTED)
    return text


def main() -> int:
    key = os.environ.get("BIOPORTAL_API_KEY", "").strip()
    if not key:
        print("BIOPORTAL_API_KEY is not set.", file=sys.stderr)
        return 1

    recorded_on = datetime.date.today().isoformat()
    recordings: dict[str, dict[str, object]] = {}
    urls_by_name: dict[str, str] = {}

    def forward(request: httpx.Request) -> httpx.Response:
        """Sends the request to BioPortal, keeps the answer, and hands the server a copy
        built the way the replay tests build theirs."""
        response = network.handle_request(request)
        response.read()
        if response.status_code != 200:
            raise RuntimeError(
                f"BioPortal answered {request.url.copy_with(query=None)} "
                f"with status {response.status_code}; nothing was recorded."
            )
        body = response.json()
        url = request.url.copy_remove_param("apikey")
        name = fixture_name(url)
        stored_url = redact(str(url), key)
        if urls_by_name.setdefault(name, stored_url) != stored_url:
            raise RuntimeError(f"Two requests map to the fixture name {name}.")
        recordings[name] = {
            "recorded_on": recorded_on,
            "request": {"method": request.method, "url": stored_url},
            "response": stored_response(url.path, response.status_code, body),
        }
        return httpx.Response(response.status_code, json=body, request=request)

    # Patching only httpx's client, not httpcore, leaves `network` free to reach BioPortal.
    with (
        httpx.HTTPTransport() as network,
        respx.mock(using="httpx", assert_all_called=False) as router,
    ):
        router.route(url__startswith=BIOPORTAL_BASE_URL).mock(side_effect=forward)
        for call in RECORDED_CALLS:
            call()

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for stale in FIXTURE_DIR.glob("*.json"):
        if stale.name not in recordings:
            stale.unlink()
    for name, recording in sorted(recordings.items()):
        text = json.dumps(recording, indent=2, ensure_ascii=False) + "\n"
        redacted = redact(text, key)
        if redacted != text:
            print(f"replaced the key with {REDACTED} in {name}")
        (FIXTURE_DIR / name).write_text(redacted, encoding="utf-8")
        print(f"recorded {name}")

    print(f"{len(recordings)} answers recorded on {recorded_on} in {FIXTURE_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
