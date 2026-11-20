import pytest
from pydantic import ValidationError

from api.v1.catalog.ai_contracts import UnitSuggestion, UnitSuggestionBatch


def test_suggestion_defaults_unit_id_to_none():
    suggestion = UnitSuggestion.model_validate({"row_number": 1})
    assert suggestion.unit_id is None


def test_suggestion_accepts_a_unit_id():
    suggestion = UnitSuggestion.model_validate({"row_number": 3, "unit_id": "11111111-1111-1111-1111-111111111111"})
    assert suggestion.unit_id == "11111111-1111-1111-1111-111111111111"


def test_suggestion_rejects_row_number_below_one():
    with pytest.raises(ValidationError):
        UnitSuggestion.model_validate({"row_number": 0})


def test_suggestion_requires_row_number():
    with pytest.raises(ValidationError):
        UnitSuggestion.model_validate({})


def test_batch_defaults_to_empty_suggestions_list():
    batch = UnitSuggestionBatch.model_validate({})
    assert batch.suggestions == []


def test_batch_parses_a_list_of_suggestions():
    batch = UnitSuggestionBatch.model_validate(
        {"suggestions": [{"row_number": 1, "unit_id": None}, {"row_number": 2, "unit_id": "abc"}]}
    )
    assert len(batch.suggestions) == 2
    assert batch.suggestions[1].row_number == 2
    assert batch.suggestions[1].unit_id == "abc"
