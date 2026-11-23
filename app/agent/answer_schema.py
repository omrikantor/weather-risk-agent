"""FinalAnswer: the structured output the model must return (the contract from the model to our code).

The model does NOT list its numbers or sources: code traces every number in answer_markdown back
to the tool result it came from (validator.trace_figures) and derives data sources from the tools
actually called. That is both faster (fewer output tokens) and more deterministic.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.tools.registry import strict_schema
from app.tools.schemas import HubId


class _Strict(BaseModel):
    """Base for answer parts: unknown fields are rejected."""
    model_config = ConfigDict(extra="forbid")


class HubMention(_Strict):
    """A hub the answer talks about. Ranks and values here are checked against the tools by the validator."""
    hub_id: HubId
    rank: int | None = Field(description="Rank from a tool result, or null.")
    severe_days_per_year: float | None = Field(description="From a tool result, or null.")
    tier: Literal["High", "Elevated", "Low"] | None
    key_drivers: list[str] = Field(description="Short phrases, e.g. 'flash flooding'.")


class Uncertainty(_Strict):
    """How certain the answer is, with reasons. The UI shows it as a confidence badge."""
    level: Literal["low", "medium", "high"]
    reasons: list[str]


class FinalAnswer(_Strict):
    """The whole answer. Only answer_markdown is shown as text; the other fields drive the UI and the checks."""
    question_type: Literal["ranking", "comparison", "statistic", "explanation", "methodology", "stand_in",
                           "clarification", "out_of_scope"]
    answer_markdown: str = Field(description="The answer shown to the user. Every number in it must be copied "
                                             "from a tool result.")
    measure_used: Literal["severe_disruption_days", "routine_disruption_days", "weather_statistic", "none"]
    hubs: list[HubMention]
    assumptions: list[str]
    uncertainty: Uncertainty
    awaiting_user_confirmation: bool = Field(
        description="True when the answer asks the user to confirm something first (e.g. analyze a stand-in hub).")
    suggested_followups: list[str]


def final_answer_schema() -> dict:
    """The FinalAnswer model as a strict JSON schema, sent to the provider with every model call."""
    return strict_schema(FinalAnswer)
