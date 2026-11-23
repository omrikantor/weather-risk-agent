"""FinalAnswer: the structured output the LLM must return (the LLM -> code contract)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.tools.registry import strict_schema
from app.tools.schemas import HubId


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HubMention(_Strict):
    hub_id: HubId
    rank: int | None = Field(description="Rank from a tool result, or null.")
    severe_days_per_year: float | None = Field(description="From a tool result, or null.")
    tier: Literal["High", "Elevated", "Low"] | None
    key_drivers: list[str] = Field(description="Short phrases, e.g. 'flash flooding'.")


class Figure(_Strict):
    label: str = Field(description="What the number is, e.g. 'Denver snow days (>=1 cm), 2025'.")
    value: float = Field(description="The number exactly as given by the tool (may be rounded for display).")
    unit: str = Field(description="e.g. 'days/yr', '%', 'storms/decade', 'km', 'cm'.")
    source_tool: str = Field(description="Name of the tool whose result contains this number.")


class Uncertainty(_Strict):
    level: Literal["low", "medium", "high"]
    reasons: list[str]


class FinalAnswer(_Strict):
    question_type: Literal["ranking", "comparison", "statistic", "explanation", "methodology", "stand_in",
                           "clarification", "out_of_scope"]
    answer_markdown: str = Field(description="The answer shown to the user (concise, structured).")
    measure_used: Literal["severe_disruption_days", "routine_disruption_days", "weather_statistic", "none"]
    hubs: list[HubMention]
    figures: list[Figure] = Field(description="EVERY number used in answer_markdown, with its source tool.")
    assumptions: list[str]
    uncertainty: Uncertainty
    data_sources: list[str]
    awaiting_user_confirmation: bool = Field(
        description="True when the answer asks the user to confirm something first (e.g. analyze a stand-in hub).")
    suggested_followups: list[str]


def final_answer_schema() -> dict:
    return strict_schema(FinalAnswer)
