"""
Tests for api/v1/suppliers/ai_client.py.

Pure helpers (no network/DB) use SimpleTestCase.
Tests that mock network I/O (OpenAI, Anthropic) use SimpleTestCase + patch.
No real API keys are ever needed.
"""

import json
from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings

from api.v1.suppliers.ai_client import (
    _anthropic_completion_content,
    _anthropic_messages_url,
    _completion_content,
    _json_fallback_max_tokens,
    _json_object_from_text,
    _json_schema_prompt,
    _provider,
    _request_ai_normalization_payload_anthropic,
    _request_ai_normalization_payload_json_fallback,
    _system_prompt,
    _use_instructor,
    request_ai_normalization_payload,
)
from api.v1.suppliers.ai_contracts import ParsedSupplierCatalog

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class _FakeUpload:
    """Minimal upload-like object — avoids DB."""

    def __init__(self):
        self.id = uuid4()
        self.supplier_id = uuid4()
        self.supplier = MagicMock()
        self.supplier.display_name = "ТестПоставщик"


VALID_CATALOG = {
    "products": [
        {
            "row_number": 1,
            "raw_name": "Болт М8",
            "normalized_name": "Болт М8",
            "price": "15.00",
        }
    ]
}

VALID_CATALOG_JSON = json.dumps(VALID_CATALOG)


def _fake_completion(content: str):
    completion = MagicMock()
    completion.choices = [MagicMock()]
    completion.choices[0].message.content = content
    return completion


# ---------------------------------------------------------------------------
# _provider
# ---------------------------------------------------------------------------


class ProviderTests(SimpleTestCase):
    def test_defaults_to_openai_when_not_set(self):
        self.assertEqual(_provider(), "openai")

    @override_settings(AI_NORMALIZATION_PROVIDER="openai")
    def test_openai_returns_openai(self):
        self.assertEqual(_provider(), "openai")

    @override_settings(AI_NORMALIZATION_PROVIDER="Anthropic")
    def test_strips_and_lowercases(self):
        self.assertEqual(_provider(), "anthropic")

    @override_settings(AI_NORMALIZATION_PROVIDER="  OpenAI  ")
    def test_strips_whitespace(self):
        self.assertEqual(_provider(), "openai")

    @override_settings(AI_NORMALIZATION_PROVIDER=None)
    def test_none_falls_back_to_openai(self):
        self.assertEqual(_provider(), "openai")

    @override_settings(AI_NORMALIZATION_PROVIDER="")
    def test_empty_string_falls_back_to_openai(self):
        self.assertEqual(_provider(), "openai")


# ---------------------------------------------------------------------------
# _use_instructor
# ---------------------------------------------------------------------------


class UseInstructorTests(SimpleTestCase):
    def test_defaults_to_false_when_not_set(self):
        self.assertFalse(_use_instructor())

    @override_settings(AI_NORMALIZATION_USE_INSTRUCTOR=True)
    def test_returns_true_when_enabled(self):
        self.assertTrue(_use_instructor())

    @override_settings(AI_NORMALIZATION_USE_INSTRUCTOR=False)
    def test_returns_false_when_disabled(self):
        self.assertFalse(_use_instructor())


# ---------------------------------------------------------------------------
# _system_prompt
# ---------------------------------------------------------------------------


class SystemPromptTests(SimpleTestCase):
    def test_normalization_phase_contains_parser_description(self):
        prompt = _system_prompt("normalization")
        self.assertIn("парсер прайс-листов", prompt)

    def test_ai_cleanup_phase_contains_revisor(self):
        prompt = _system_prompt("ai_cleanup")
        self.assertIn("ревизор", prompt)

    def test_unknown_phase_returns_normalization_prompt(self):
        prompt = _system_prompt("unknown_phase")
        self.assertIn("парсер прайс-листов", prompt)

    def test_normalization_and_cleanup_are_different(self):
        self.assertNotEqual(_system_prompt("normalization"), _system_prompt("ai_cleanup"))

    def test_ai_cleanup_prompt_does_not_ask_to_fix_price_or_multiplicity(self):
        prompt = _system_prompt("ai_cleanup").lower()

        self.assertNotIn("кратности", prompt)
        self.assertIn("не изменяй цену", prompt)


# ---------------------------------------------------------------------------
# _json_schema_prompt
# ---------------------------------------------------------------------------


class JsonSchemaPromptTests(SimpleTestCase):
    def test_contains_required_fields(self):
        prompt = _json_schema_prompt()
        self.assertIn("row_number", prompt)
        self.assertIn("normalized_name", prompt)
        self.assertIn("Return only valid JSON", prompt)

    def test_returns_string(self):
        self.assertIsInstance(_json_schema_prompt(), str)


# ---------------------------------------------------------------------------
# _anthropic_messages_url
# ---------------------------------------------------------------------------


class AnthropicMessagesUrlTests(SimpleTestCase):
    def test_none_returns_default_url(self):
        url = _anthropic_messages_url(None)
        self.assertEqual(url, "https://api.anthropic.com/v1/messages")

    def test_empty_string_returns_default_url(self):
        url = _anthropic_messages_url("")
        self.assertEqual(url, "https://api.anthropic.com/v1/messages")

    def test_base_without_messages_appends_messages(self):
        url = _anthropic_messages_url("https://custom.host/v1")
        self.assertEqual(url, "https://custom.host/v1/messages")

    def test_base_with_trailing_slash_strips_then_appends(self):
        url = _anthropic_messages_url("https://custom.host/v1/")
        self.assertEqual(url, "https://custom.host/v1/messages")

    def test_base_already_ending_with_messages_unchanged(self):
        url = _anthropic_messages_url("https://custom.host/v1/messages")
        self.assertEqual(url, "https://custom.host/v1/messages")

    def test_base_with_messages_and_trailing_slash_normalised(self):
        url = _anthropic_messages_url("https://custom.host/v1/messages/")
        self.assertEqual(url, "https://custom.host/v1/messages")


# ---------------------------------------------------------------------------
# _anthropic_completion_content
# ---------------------------------------------------------------------------


class AnthropicCompletionContentTests(SimpleTestCase):
    def test_extracts_single_text_part(self):
        payload = {"content": [{"type": "text", "text": "hello world"}]}
        self.assertEqual(_anthropic_completion_content(payload), "hello world")

    def test_joins_multiple_text_parts(self):
        payload = {
            "content": [
                {"type": "text", "text": "foo"},
                {"type": "text", "text": "bar"},
            ]
        }
        self.assertEqual(_anthropic_completion_content(payload), "foobar")

    def test_ignores_non_text_parts(self):
        payload = {
            "content": [
                {"type": "image", "url": "http://example.com/img.png"},
                {"type": "text", "text": "actual text"},
            ]
        }
        self.assertEqual(_anthropic_completion_content(payload), "actual text")

    def test_raises_on_empty_content_list(self):
        with self.assertRaises(ValueError, msg="empty content list should raise"):
            _anthropic_completion_content({"content": []})

    def test_raises_on_missing_content_key(self):
        with self.assertRaises((ValueError, KeyError, TypeError)):
            _anthropic_completion_content({})

    def test_raises_when_only_non_text_parts(self):
        payload = {"content": [{"type": "image", "url": "x"}]}
        with self.assertRaises(ValueError):
            _anthropic_completion_content(payload)

    def test_strips_surrounding_whitespace(self):
        payload = {"content": [{"type": "text", "text": "  trimmed  "}]}
        self.assertEqual(_anthropic_completion_content(payload), "trimmed")


# ---------------------------------------------------------------------------
# _json_fallback_max_tokens
# ---------------------------------------------------------------------------


class JsonFallbackMaxTokensTests(SimpleTestCase):
    def test_empty_list_returns_minimum(self):
        self.assertEqual(_json_fallback_max_tokens([]), 500)

    def test_single_row_returns_minimum(self):
        # 1 * 260 + 240 = 500 == minimum
        self.assertEqual(_json_fallback_max_tokens([{}]), 500)

    def test_two_rows(self):
        # 2 * 260 + 240 = 760
        self.assertEqual(_json_fallback_max_tokens([{}, {}]), 760)

    def test_many_rows_capped_at_4000(self):
        # 20 * 260 + 240 = 5440 → capped to 4000
        result = _json_fallback_max_tokens([{}] * 20)
        self.assertEqual(result, 4000)

    def test_at_cap_boundary(self):
        # n * 260 + 240 = 4000 → n = 14.46 → at n=15 → 4140 > 4000 → cap
        # at n=14 → 14 * 260 + 240 = 3880 < 4000 → no cap
        self.assertEqual(_json_fallback_max_tokens([{}] * 14), 3880)
        self.assertEqual(_json_fallback_max_tokens([{}] * 15), 4000)


# ---------------------------------------------------------------------------
# _completion_content
# ---------------------------------------------------------------------------


class CompletionContentTests(SimpleTestCase):
    def test_returns_content_from_first_choice(self):
        completion = _fake_completion("some response")
        self.assertEqual(_completion_content(completion), "some response")

    def test_raises_when_no_choices(self):
        completion = MagicMock()
        completion.choices = []
        with self.assertRaises(ValueError):
            _completion_content(completion)

    def test_raises_when_choices_is_none(self):
        completion = MagicMock()
        completion.choices = None
        with self.assertRaises(ValueError):
            _completion_content(completion)

    def test_raises_when_content_empty(self):
        completion = MagicMock()
        completion.choices = [MagicMock()]
        completion.choices[0].message.content = ""
        with self.assertRaises(ValueError):
            _completion_content(completion)

    def test_raises_when_content_none(self):
        completion = MagicMock()
        completion.choices = [MagicMock()]
        completion.choices[0].message.content = None
        with self.assertRaises(ValueError):
            _completion_content(completion)


# ---------------------------------------------------------------------------
# _json_object_from_text
# ---------------------------------------------------------------------------


class JsonObjectFromTextTests(SimpleTestCase):
    def test_clean_json_parsed(self):
        obj = _json_object_from_text('{"key": "value"}')
        self.assertEqual(obj["key"], "value")

    def test_strips_json_fence(self):
        fenced = '```json\n{"a": 1}\n```'
        obj = _json_object_from_text(fenced)
        self.assertEqual(obj["a"], 1)

    def test_strips_plain_code_fence(self):
        fenced = '```\n{"a": 2}\n```'
        obj = _json_object_from_text(fenced)
        self.assertEqual(obj["a"], 2)

    def test_extracts_json_embedded_in_text(self):
        text = 'Here is the result: {"products": []} end of output'
        obj = _json_object_from_text(text)
        self.assertEqual(obj["products"], [])

    def test_raises_when_no_json_at_all(self):
        with self.assertRaises(Exception):
            _json_object_from_text("no json here at all")

    def test_empty_string_raises(self):
        with self.assertRaises(Exception):
            _json_object_from_text("")

    def test_none_treated_as_empty_string(self):
        with self.assertRaises(Exception):
            _json_object_from_text(None)

    def test_json_array_parsed_successfully(self):
        # json.loads accepts arrays — function returns whatever json.loads returns
        result = _json_object_from_text("[1, 2, 3]")
        self.assertEqual(result, [1, 2, 3])


# ---------------------------------------------------------------------------
# _request_ai_normalization_payload_json_fallback
# ---------------------------------------------------------------------------


class JsonFallbackTests(SimpleTestCase):
    def _run(self, content: str, input_rows=None):
        upload = _FakeUpload()
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _fake_completion(content)
        messages = [{"role": "user", "content": "test"}]
        return _request_ai_normalization_payload_json_fallback(
            client=mock_client,
            model="gpt-4o",
            phase="normalization",
            upload=upload,
            input_rows=input_rows or [{"row_number": 1, "name": "Болт"}],
            messages=messages,
            base_url=None,
        )

    def test_valid_response_returns_parsed_catalog(self):
        result = self._run(VALID_CATALOG_JSON)
        self.assertIsInstance(result, ParsedSupplierCatalog)
        self.assertEqual(len(result.products), 1)
        self.assertEqual(result.products[0].row_number, 1)

    def test_passes_temperature_zero_to_api(self):
        upload = _FakeUpload()
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _fake_completion(VALID_CATALOG_JSON)
        _request_ai_normalization_payload_json_fallback(
            client=mock_client,
            model="gpt-4o",
            phase="normalization",
            upload=upload,
            input_rows=[{}],
            messages=[],
            base_url=None,
        )
        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        self.assertEqual(call_kwargs["temperature"], 0)

    def test_fenced_json_response_is_parsed(self):
        fenced = "```json\n" + VALID_CATALOG_JSON + "\n```"
        result = self._run(fenced)
        self.assertIsInstance(result, ParsedSupplierCatalog)

    def test_invalid_schema_raises_validation_error(self):
        from pydantic import ValidationError

        bad_json = json.dumps({"products": [{"row_number": "not_an_int", "raw_name": "x", "normalized_name": "y"}]})
        with self.assertRaises(ValidationError):
            self._run(bad_json)

    def test_non_json_content_raises(self):
        with self.assertRaises(Exception):
            self._run("this is plain text, not json")

    def test_empty_products_list_is_valid(self):
        result = self._run(json.dumps({"products": []}))
        self.assertEqual(result.products, [])


# ---------------------------------------------------------------------------
# _request_ai_normalization_payload_anthropic
# ---------------------------------------------------------------------------


class AnthropicRequestTests(SimpleTestCase):
    def _urlopen_mock(self, response_body: dict):
        response_bytes = json.dumps(response_body).encode("utf-8")
        mock_response = MagicMock()
        mock_response.read.return_value = response_bytes
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        return mock_response

    @override_settings(AI_NORMALIZATION_ANTHROPIC_VERSION="2023-06-01")
    def test_valid_response_returns_parsed_catalog(self):
        upload = _FakeUpload()
        anthropic_body = {"content": [{"type": "text", "text": VALID_CATALOG_JSON}]}
        with patch("urllib.request.urlopen", return_value=self._urlopen_mock(anthropic_body)):
            result = _request_ai_normalization_payload_anthropic(
                api_key="test-key",
                base_url=None,
                model="claude-opus-4-8",
                timeout_seconds=30,
                phase="normalization",
                upload=upload,
                input_rows=[{"row_number": 1, "name": "Болт"}],
                system_prompt=_system_prompt("normalization"),
            )
        self.assertIsInstance(result, ParsedSupplierCatalog)
        self.assertEqual(len(result.products), 1)

    @override_settings(AI_NORMALIZATION_ANTHROPIC_VERSION="2023-06-01")
    def test_sends_correct_headers(self):
        upload = _FakeUpload()
        anthropic_body = {"content": [{"type": "text", "text": VALID_CATALOG_JSON}]}
        with patch("urllib.request.urlopen", return_value=self._urlopen_mock(anthropic_body)):
            with patch("urllib.request.Request") as mock_request_cls:
                mock_request_cls.return_value = MagicMock()
                _request_ai_normalization_payload_anthropic(
                    api_key="secret-key",
                    base_url=None,
                    model="claude-opus-4-8",
                    timeout_seconds=30,
                    phase="normalization",
                    upload=upload,
                    input_rows=[],
                    system_prompt="system",
                )
        headers = mock_request_cls.call_args.kwargs.get("headers") or mock_request_cls.call_args[1].get("headers", {})
        self.assertIn("Authorization", headers)
        self.assertIn("secret-key", headers["Authorization"])

    @override_settings(AI_NORMALIZATION_ANTHROPIC_VERSION="2023-06-01")
    def test_uses_custom_base_url(self):
        upload = _FakeUpload()
        anthropic_body = {"content": [{"type": "text", "text": VALID_CATALOG_JSON}]}
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(anthropic_body).encode()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            _request_ai_normalization_payload_anthropic(
                api_key="key",
                base_url="https://proxy.example.com/v1",
                model="claude-haiku-4-5-20251001",
                timeout_seconds=10,
                phase="normalization",
                upload=upload,
                input_rows=[],
                system_prompt="sys",
            )
        self.assertIn("proxy.example.com", captured["url"])

    @override_settings(AI_NORMALIZATION_ANTHROPIC_VERSION="2023-06-01")
    def test_empty_content_raises_value_error(self):
        upload = _FakeUpload()
        anthropic_body = {"content": []}
        with patch("urllib.request.urlopen", return_value=self._urlopen_mock(anthropic_body)):
            with self.assertRaises(ValueError):
                _request_ai_normalization_payload_anthropic(
                    api_key="key",
                    base_url=None,
                    model="claude-opus-4-8",
                    timeout_seconds=10,
                    phase="normalization",
                    upload=upload,
                    input_rows=[],
                    system_prompt="sys",
                )


# ---------------------------------------------------------------------------
# request_ai_normalization_payload — routing logic
# ---------------------------------------------------------------------------


class RequestAINormalizationPayloadTests(SimpleTestCase):
    def _catalog_json_dump(self):
        return VALID_CATALOG_JSON

    def _urlopen_mock(self, response_body: dict):
        response_bytes = json.dumps(response_body).encode("utf-8")
        mock_response = MagicMock()
        mock_response.read.return_value = response_bytes
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        return mock_response

    @override_settings(AI_NORMALIZATION_PROVIDER="openai", AI_NORMALIZATION_USE_INSTRUCTOR=False)
    def test_openai_no_instructor_calls_json_fallback(self):
        upload = _FakeUpload()
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _fake_completion(VALID_CATALOG_JSON)
        with patch("api.v1.suppliers.ai_client.OpenAI", return_value=mock_client):
            result = request_ai_normalization_payload(
                api_key="test",
                base_url=None,
                model="gpt-4o",
                timeout_seconds=30,
                upload=upload,
                input_rows=[{"row_number": 1, "name": "Болт"}],
            )
        self.assertIn("products", result)
        self.assertEqual(len(result["products"]), 1)

    @override_settings(
        AI_NORMALIZATION_PROVIDER="openai",
        AI_NORMALIZATION_USE_INSTRUCTOR=False,
    )
    def test_passes_base_url_to_openai_client_when_set(self):
        upload = _FakeUpload()
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _fake_completion(VALID_CATALOG_JSON)
        with patch("api.v1.suppliers.ai_client.OpenAI", return_value=mock_client) as mock_cls:
            request_ai_normalization_payload(
                api_key="test",
                base_url="https://openai-proxy.example.com",
                model="gpt-4o",
                timeout_seconds=30,
                upload=upload,
                input_rows=[],
            )
        init_kwargs = mock_cls.call_args.kwargs
        self.assertEqual(init_kwargs["base_url"], "https://openai-proxy.example.com")

    @override_settings(
        AI_NORMALIZATION_PROVIDER="openai",
        AI_NORMALIZATION_USE_INSTRUCTOR=False,
    )
    def test_no_base_url_not_passed_to_openai_client(self):
        upload = _FakeUpload()
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _fake_completion(VALID_CATALOG_JSON)
        with patch("api.v1.suppliers.ai_client.OpenAI", return_value=mock_client) as mock_cls:
            request_ai_normalization_payload(
                api_key="test",
                base_url=None,
                model="gpt-4o",
                timeout_seconds=30,
                upload=upload,
                input_rows=[],
            )
        init_kwargs = mock_cls.call_args.kwargs
        self.assertNotIn("base_url", init_kwargs)

    @override_settings(
        AI_NORMALIZATION_PROVIDER="openai",
        AI_NORMALIZATION_USE_INSTRUCTOR=True,
    )
    def test_openai_with_instructor_calls_instructor(self):
        upload = _FakeUpload()
        catalog = ParsedSupplierCatalog.model_validate(VALID_CATALOG)
        mock_instructor = MagicMock()
        mock_instructor.chat.completions.create.return_value = catalog
        with patch("api.v1.suppliers.ai_client.OpenAI", return_value=MagicMock()):
            with patch("instructor.from_openai", return_value=mock_instructor):
                result = request_ai_normalization_payload(
                    api_key="test",
                    base_url=None,
                    model="gpt-4o",
                    timeout_seconds=30,
                    upload=upload,
                    input_rows=[{"row_number": 1, "name": "Болт"}],
                )
        mock_instructor.chat.completions.create.assert_called_once()
        self.assertIn("products", result)

    @override_settings(
        AI_NORMALIZATION_PROVIDER="openai",
        AI_NORMALIZATION_USE_INSTRUCTOR=True,
    )
    def test_instructor_failure_falls_back_to_json(self):
        upload = _FakeUpload()
        mock_openai_client = MagicMock()
        mock_openai_client.chat.completions.create.return_value = _fake_completion(VALID_CATALOG_JSON)
        mock_instructor = MagicMock()
        mock_instructor.chat.completions.create.side_effect = Exception("instructor failed")
        with patch("api.v1.suppliers.ai_client.OpenAI", return_value=mock_openai_client):
            with patch("instructor.from_openai", return_value=mock_instructor):
                result = request_ai_normalization_payload(
                    api_key="test",
                    base_url=None,
                    model="gpt-4o",
                    timeout_seconds=30,
                    upload=upload,
                    input_rows=[{"row_number": 1, "name": "Болт"}],
                )
        # Fallback to json path succeeded
        self.assertIn("products", result)

    @override_settings(
        AI_NORMALIZATION_PROVIDER="anthropic",
        AI_NORMALIZATION_ANTHROPIC_VERSION="2023-06-01",
    )
    def test_anthropic_provider_calls_urlopen(self):
        upload = _FakeUpload()
        anthropic_body = {"content": [{"type": "text", "text": VALID_CATALOG_JSON}]}
        with patch("urllib.request.urlopen", return_value=self._urlopen_mock(anthropic_body)) as mock_open:
            result = request_ai_normalization_payload(
                api_key="anthropic-key",
                base_url=None,
                model="claude-opus-4-8",
                timeout_seconds=30,
                upload=upload,
                input_rows=[{"row_number": 1, "name": "Болт"}],
            )
        mock_open.assert_called_once()
        self.assertIn("products", result)

    @override_settings(AI_NORMALIZATION_PROVIDER="anthropic", AI_NORMALIZATION_ANTHROPIC_VERSION="2023-06-01")
    def test_result_is_json_serializable_dict(self):
        upload = _FakeUpload()
        anthropic_body = {"content": [{"type": "text", "text": VALID_CATALOG_JSON}]}
        with patch("urllib.request.urlopen", return_value=self._urlopen_mock(anthropic_body)):
            result = request_ai_normalization_payload(
                api_key="key",
                base_url=None,
                model="claude-opus-4-8",
                timeout_seconds=30,
                upload=upload,
                input_rows=[{"row_number": 1, "name": "Болт"}],
            )
        # model_dump(mode="json") should produce JSON-serializable dict
        json.dumps(result)  # must not raise

    @override_settings(AI_NORMALIZATION_PROVIDER="openai", AI_NORMALIZATION_USE_INSTRUCTOR=False)
    def test_unit_codes_are_embedded_in_system_prompt_when_provided(self):
        upload = _FakeUpload()
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _fake_completion(VALID_CATALOG_JSON)
        with patch("api.v1.suppliers.ai_client.OpenAI", return_value=mock_client) as mock_cls:
            request_ai_normalization_payload(
                api_key="test",
                base_url=None,
                model="gpt-4o",
                timeout_seconds=30,
                upload=upload,
                input_rows=[],
                unit_codes=["шт", "кг", "уп", "кор"],
            )
        sent_system_message = mock_cls.return_value.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        self.assertIn("уп", sent_system_message)
        self.assertIn("кор", sent_system_message)
