"""ТЗ §8-10: the model may ONLY ever propose unit (a code from a closed list) and
country_of_origin -- this schema has no field for price/multiplicity/min
qty/availability, so the model has no slot to put a guess in even if it
tried. Mirrors api.v1.catalog.ai_contracts.CandidateRankingBatch's shape."""

from pydantic import BaseModel, ConfigDict, Field


class SupplierProductEnrichment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    row_number: int = Field(description="Original import row number.", ge=1)
    unit: str | None = Field(
        default=None, description="Code of the unit the item is SOLD in, from the given units list. Only if needs_unit."
    )
    country_of_origin: str | None = Field(
        default=None,
        description="Only if needs_country and explicitly stated in the source text -- never inferred from stereotype.",
    )


class SupplierProductEnrichmentBatch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[SupplierProductEnrichment] = Field(default_factory=list)
