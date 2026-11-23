"""Tool input models: the contract for what the language model may send to each tool.

Each model becomes a strict OpenAI function schema (see registry.strict_schema): every field
is required (optional ones are nullable) and no extra fields are allowed. Hub ids and regions
are enums, so the model cannot ask for a hub that doesn't exist.
"""

from datetime import date
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.ingest.hubs import load_hubs

# Built from data/hubs.yaml at start-up, so adding a hub to the file adds it to every tool schema.
HubId = Enum("HubId", {h["id"]: h["id"] for h in load_hubs()}, type=str)
Region = Literal["Midwest", "South", "West", "Northeast"]
Hazard = Literal["winter", "flood", "convective", "hurricane", "heat"]


class _Strict(BaseModel):
    """Base for all inputs: unknown fields are rejected instead of silently ignored."""
    model_config = ConfigDict(extra="forbid")


class ListHubsInput(_Strict):
    region: Region | None = Field(description="US Census region to filter by, or null for all hubs.")


class DayCosts(_Strict):
    """Optional analyst weights: how costly one disaster-level day of each hazard is, relative to the others."""
    winter: float | None = Field(description="Relative cost of a winter disruption day (null = 1).")
    flood: float | None = Field(description="Relative cost of a flood disruption day (null = 1).")
    convective: float | None = Field(description="Relative cost of a convective/wind disruption day (null = 1).")
    hurricane: float | None = Field(description="Relative cost of a hurricane disruption day (null = 1).")
    heat: float | None = Field(description="Relative cost of a heat disruption day (null = 1).")

    @field_validator("winter", "flood", "convective", "hurricane", "heat")
    @classmethod
    def _positive(cls, v):
        # A zero or negative cost would silently remove a hazard or reverse the ranking.
        if v is not None and v <= 0:
            raise ValueError("day costs must be greater than 0")
        return v


class RankHubsInput(_Strict):
    """Arguments for rank_hubs."""
    measure: Literal["severe_disruption_days", "routine_disruption_days"] = Field(
        description="severe_disruption_days (default ranking for resilience investment) or "
                    "routine_disruption_days (all NWS warning days).")
    hazard: Literal["all", "winter", "flood", "convective", "hurricane", "heat"] = Field(
        description="'all' for the total across hazards, or a single hazard.")
    region: Region | None = Field(description="US Census region filter, or null for all regions.")
    top_n: int | None = Field(description="How many hubs to return (1-47), or null for all.")
    day_costs: DayCosts | None = Field(
        description="Optional analyst-supplied relative cost per disruption day by hazard (only with hazard='all'). "
                    "Null = every day counts the same.")

    @field_validator("top_n")
    @classmethod
    def _top_n(cls, v):
        if v is not None and not 1 <= v <= 47:
            raise ValueError("top_n must be between 1 and 47")
        return v

    @model_validator(mode="after")
    def _costs_only_for_all(self):
        # With a single hazard, multiplying every hub by the same cost can't change the order,
        # so accepting costs would let the answer claim a weighting that had no effect.
        if self.day_costs is not None and self.hazard != "all":
            raise ValueError("day_costs only applies when hazard = 'all'; set day_costs to null")
        return self


class CompareHubsInput(_Strict):
    """Arguments for compare_hubs."""
    hub_ids: list[HubId] = Field(description="2 to 6 hub ids to compare.")
    hazards: list[Hazard] | None = Field(description="Hazards to compare, or null for all five.")

    @field_validator("hub_ids")
    @classmethod
    def _n(cls, v):
        if not 2 <= len(v) <= 6 or len(set(v)) != len(v):
            raise ValueError("give 2 to 6 distinct hub ids")
        return v


class HubBreakdownInput(_Strict):
    """Arguments for get_hub_risk_breakdown."""
    hub_id: HubId = Field(description="The hub to explain.")


# Every statistic get_weather_stat can compute; the thresholds are explained in the field description below.
WeatherMetric = Literal[
    "snow_day", "trace_snow_day", "heavy_snow_day", "ice_day", "freezing_day", "extreme_cold_day",
    "heavy_rain_day", "rain_day", "extreme_heat_day", "damaging_gust_day", "thunder_day",
    "total_precipitation_mm", "total_snowfall_cm",
    "warning_days_winter", "warning_days_flood", "warning_days_convective", "warning_days_hurricane",
    "warning_days_heat",
]


class WeatherStatInput(_Strict):
    """Arguments for get_weather_stat: one metric at one hub over a date range."""
    hub_id: HubId = Field(description="The hub.")
    metric: WeatherMetric = Field(
        description="snow_day: snowfall >= 1 cm; trace_snow_day: >= 0.1 cm; heavy_snow_day: >= 10 cm; "
                    "ice_day: observed glaze/freezing rain/ice pellets; freezing_day: low <= 0 C; "
                    "extreme_cold_day: low <= -15 C; rain_day: precipitation >= 1 mm; heavy_rain_day: >= 50 mm; "
                    "extreme_heat_day: high >= 38 C; damaging_gust_day: gust >= 93 km/h; thunder_day: observed "
                    "thunder; total_*: sums; warning_days_<hazard>: days with an NWS warning (2006+).")
    start_date: date = Field(description="First day, YYYY-MM-DD (data from 1996-01-01).")
    end_date: date = Field(description="Last day, YYYY-MM-DD (inclusive).")

    @model_validator(mode="after")
    def _range(self):
        if self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        if self.start_date < date(1996, 1, 1):
            raise ValueError("data start on 1996-01-01")
        return self


class MethodologyInput(_Strict):
    """get_methodology takes no arguments."""


class SeasonalOutlookInput(_Strict):
    """Arguments for get_seasonal_outlook."""
    month: int | None = Field(description="Calendar month 1-12, or null for the current month.")
    region: Region | None = Field(description="US Census region filter, or null for all hubs.")
    top_n: int | None = Field(description="How many hubs to return (1-47), or null for 5.")

    @field_validator("month")
    @classmethod
    def _month(cls, v):
        if v is not None and not 1 <= v <= 12:
            raise ValueError("month must be 1-12")
        return v

    @field_validator("top_n")
    @classmethod
    def _top(cls, v):
        if v is not None and not 1 <= v <= 47:
            raise ValueError("top_n must be between 1 and 47")
        return v


class LiveWarningsInput(_Strict):
    """Arguments for get_live_warnings."""
    region: Region | None = Field(description="US Census region filter, or null for all hubs.")


class FindNearestHubsInput(_Strict):
    """Arguments for find_nearest_hubs: free text, resolved against Census places in locate.py."""
    location: str = Field(description="A US place that is not one of our hubs, e.g. 'Tampa, FL' or 'Spokane'.")
