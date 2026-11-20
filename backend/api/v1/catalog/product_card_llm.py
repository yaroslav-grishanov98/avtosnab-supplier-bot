from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from api.llm import LLMConnector


class UnitOption(BaseModel):
    id: UUID
    code: str
    name: str


class ProductCandidate(BaseModel):
    id: UUID
    name: str
    unit_id: UUID | None = None


class ProductCardSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    normalized_name: str = Field(min_length=1, max_length=500)
    suggested_unit_id: UUID | None = None
    candidate_product_ids: list[UUID] = Field(default_factory=list, max_length=10)

    @field_validator("candidate_product_ids")
    @classmethod
    def unique_candidate_ids(cls, value):
        return list(dict.fromkeys(value))


PRODUCT_CARD_PROMPT = (
    "Ты помощник карточки товара AutoSnab. Только предлагай значения, не принимай финальных решений. "
    "Не заменяй товар, не выбирай 'не добавлять' и не назначай окончательное совпадение. "
    "normalized_name нужен только для поиска; исходное название не изменяется. "
    "suggested_unit_id выбирай только из allowed_units и только если input.unit_id отсутствует. "
    "candidate_product_ids выбирай и ранжируй только из candidates. Верни несколько лучших, а не одно финальное решение."
)


def suggest_product_card(
    *,
    name: str,
    unit_id: UUID | None,
    allowed_units: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    connector: LLMConnector | None = None,
) -> ProductCardSuggestion:
    """Return a validated recommendation without mutating any product."""
    units = [UnitOption.model_validate(unit) for unit in allowed_units]
    product_candidates = [ProductCandidate.model_validate(candidate) for candidate in candidates]
    payload = {
        "input": {"name": name, "unit_id": str(unit_id) if unit_id else None},
        "allowed_units": [unit.model_dump(mode="json") for unit in units],
        "candidates": [candidate.model_dump(mode="json") for candidate in product_candidates],
    }
    result = (connector or LLMConnector()).structured(
        scenario="product_card_suggestion",
        system_prompt=PRODUCT_CARD_PROMPT,
        payload=payload,
        response_model=ProductCardSuggestion,
    )

    allowed_unit_ids = {unit.id for unit in units}
    allowed_candidate_ids = {candidate.id for candidate in product_candidates}
    if unit_id is not None and result.suggested_unit_id is not None:
        raise ValueError("LLM must not replace an existing unit.")
    if result.suggested_unit_id is not None and result.suggested_unit_id not in allowed_unit_ids:
        raise ValueError("LLM returned a unit outside allowed_units.")
    if not set(result.candidate_product_ids).issubset(allowed_candidate_ids):
        raise ValueError("LLM returned a product outside candidates.")
    return result
