"""Tool registry: OpenAI strict function schemas + validated execution.

The same functions are exposed to the chat agent (OpenAI function calling) and, later, to the
MCP server, so there is one implementation of every tool.
"""

import copy
import json
from dataclasses import dataclass
from typing import Callable

from pydantic import BaseModel, ValidationError

from app.tools import hub_tools as t
from app.tools import schemas as s


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    fn: Callable


TOOLS: dict[str, Tool] = {tool.name: tool for tool in [
    Tool("list_hubs", "List the hubs in the network (optionally one US Census region).", s.ListHubsInput, t.list_hubs),
    Tool("rank_hubs", "Rank hubs by severe or routine disruption days, overall or for one hazard, optionally "
                      "within a region and with analyst-supplied day costs.", s.RankHubsInput, t.rank_hubs),
    Tool("compare_hubs", "Compare 2-6 hubs hazard by hazard: severe and routine disruption days, severity shares "
                         "and supporting evidence (storms, snow, rain, reports).", s.CompareHubsInput, t.compare_hubs),
    Tool("get_hub_risk_breakdown", "Explain one hub's risk: rank, tier, contribution of each hazard, evidence, "
                                   "recent FEMA disasters, station and confidence.", s.HubBreakdownInput,
         t.get_hub_risk_breakdown),
    Tool("get_weather_stat", "Count days meeting a weather condition (or totals) at a hub over any date range "
                             "from 1996, e.g. % of days with snowfall last year.", s.WeatherStatInput,
         t.get_weather_stat),
    Tool("get_methodology", "How scores are computed: KPI definitions, severity shares, data sources, assumptions "
                            "and limitations.", s.MethodologyInput, t.get_methodology),
    Tool("get_seasonal_outlook", "Which hubs historically face the most disruption in a given calendar month "
                                 "(default: this month), the main hazard, how often it happened (% of years), and "
                                 "past FEMA disasters in that month. Historical frequency, not a forecast.",
         s.SeasonalOutlookInput, t.get_seasonal_outlook),
    Tool("find_nearest_hubs", "For a US place that is NOT one of our hubs: locate it and recommend the most similar "
                              "nearby hub as a stand-in (hazard-profile similarity + distance).",
         s.FindNearestHubsInput, t.find_nearest_hubs),
]}

_DROP_KEYS = {"title", "default", "format", "minItems", "maxItems", "minimum", "maximum"}


def strict_schema(model: type[BaseModel]) -> dict:
    """Pydantic JSON schema -> OpenAI strict schema: refs inlined, every property required,
    no additional properties, unsupported keywords removed (Pydantic still validates them)."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(copy.deepcopy(defs[node["$ref"].split("/")[-1]]))
            node = {k: walk(v) for k, v in node.items() if k not in _DROP_KEYS}
            if node.get("type") == "object" or "properties" in node:
                node["type"] = "object"
                node.setdefault("properties", {})
                node["required"] = list(node["properties"])
                node["additionalProperties"] = False
            return node
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(schema)


def openai_tools() -> list[dict]:
    """Function-tool definitions for the OpenAI Responses API."""
    return [{"type": "function", "name": tool.name, "description": tool.description,
             "parameters": strict_schema(tool.input_model), "strict": True} for tool in TOOLS.values()]


def execute(name: str, arguments: str | dict, conn=None) -> dict:
    """Validate arguments and run a tool. Errors are returned as data, never raised to the agent loop."""
    tool = TOOLS.get(name)
    if tool is None:
        return {"error": f"unknown tool '{name}'", "hint": f"available tools: {sorted(TOOLS)}"}
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
        parsed = tool.input_model.model_validate(args)
    except (json.JSONDecodeError, ValidationError) as e:
        return {"error": "invalid arguments", "details": str(e)[:800],
                "hint": "fix the arguments to match the tool schema and call again"}
    try:
        return tool.fn(parsed, conn)
    except Exception as e:  # noqa: BLE001 - surfaced to the model as a tool error
        return {"error": f"tool failed: {type(e).__name__}: {e}"[:800]}
