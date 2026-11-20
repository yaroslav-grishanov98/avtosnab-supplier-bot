from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class UnitSuggestion(BaseModel):
    model_config = ConfigDict(extra="ignore")

    row_number: int = Field(description="Original preview row number.", ge=1)
    unit_id: Optional[str] = Field(
        default=None,
        description="Unit id from the closed list the caller supplied, or null when not confident.",
    )


class UnitSuggestionBatch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    suggestions: list[UnitSuggestion] = Field(default_factory=list)


class CandidateRanking(BaseModel):
    model_config = ConfigDict(extra="ignore")

    row_number: int = Field(description="Original preview row number.", ge=1)
    ranked_candidate_ids: list[str] = Field(
        default_factory=list,
        description="Subset of the ids offered for this row, best match first. Empty if none look like a confident match.",
    )


class CandidateRankingBatch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    rankings: list[CandidateRanking] = Field(default_factory=list)
