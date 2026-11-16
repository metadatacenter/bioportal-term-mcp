"""
What a calling model reads about each tool, listed through FastMCP as a client would see it.

The other tests call the tool functions directly, so they never see what FastMCP publishes: the
registered tools, the descriptions it takes from the docstrings, and the parameter schemas it
derives from the type hints. A tool's description is the only documentation the model reads, so
these tests hold each one to the facts a test can check.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from bioportal_term_mcp import server

EXPECTED_TOOLS = {
    "ping",
    "get_ontology",
    "find_ontology",
    "get_class",
    "find_class",
    "get_value_set",
    "find_value_set",
}

# A word shaped like one of this server's tool names, such as find_class or get_value_set.
TOOL_NAME = re.compile(r"\b(?:get|find)_[a-z_]+\b")


def _listed_tools():
    return {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}


def test_every_tool_is_registered():
    assert set(_listed_tools()) == EXPECTED_TOOLS


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_each_tool_has_a_description(name: str):
    description = _listed_tools()[name].description
    assert description is not None and description.strip()


# Each tool's parameters as the published schema states them: every parameter, and those a call must supply.
EXPECTED_PARAMETERS = {
    "ping": ({"message"}, {"message"}),
    "get_ontology": ({"acronym"}, {"acronym"}),
    "find_ontology": ({"query", "max_results"}, {"query"}),
    "get_class": ({"class_iri", "ontology_acronym"}, {"class_iri", "ontology_acronym"}),
    "find_class": ({"query", "ontology_acronym", "max_results"}, {"query"}),
    "get_value_set": ({"value_set_iri", "vs_collection"}, {"value_set_iri", "vs_collection"}),
    "find_value_set": ({"query", "vs_collection", "max_results"}, {"query", "vs_collection"}),
}


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_each_parameter_schema_states_the_parameters_and_which_are_required(name: str):
    schema = _listed_tools()[name].inputSchema
    parameters, required = EXPECTED_PARAMETERS[name]
    assert set(schema["properties"]) == parameters
    assert set(schema.get("required", [])) == required


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_each_tool_a_description_names_exists(name: str):
    tool = _listed_tools()[name]
    named = set(TOOL_NAME.findall(tool.description or "")) | set(TOOL_NAME.findall(str(tool.inputSchema)))
    assert named <= EXPECTED_TOOLS, f"{name} names {sorted(named - EXPECTED_TOOLS)}, which are no tools"
