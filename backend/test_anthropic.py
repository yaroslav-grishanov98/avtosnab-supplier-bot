import json
import os
import time

import requests
from dotenv import load_dotenv

TIMEOUT_SECONDS = 35


def mask_secret(value: str | None, visible: int = 4) -> str:
    if not value:
        return "<empty>"
    if len(value) <= visible:
        return "*" * len(value)
    return f"{value[:visible]}{'*' * (len(value) - visible)}"


def get_model() -> str | None:
    return (
        os.getenv("MODEL")
        or os.getenv("ANTHROPIC_MODEL")
        or os.getenv("AI_MODEL")
        or os.getenv("AI_NORMALIZATION_MODEL")
    )


def main() -> None:
    print("Loading .env with override=True...")
    load_dotenv(override=True)

    base_url = os.getenv("BASE_URL") or os.getenv("AI_NORMALIZATION_URL")
    api_key = os.getenv("API_KEY") or os.getenv("AI_NORMALIZATION_API_KEY")
    anthropic_version = os.getenv("AI_NORMALIZATION_ANTHROPIC_VERSION")
    model = get_model()

    missing = []
    if not base_url:
        missing.append("BASE_URL / AI_NORMALIZATION_URL")
    if not api_key:
        missing.append("API_KEY / AI_NORMALIZATION_API_KEY")
    if not anthropic_version:
        missing.append("AI_NORMALIZATION_ANTHROPIC_VERSION")
    if not model:
        missing.append("MODEL / ANTHROPIC_MODEL / AI_MODEL / AI_NORMALIZATION_MODEL")

    if missing:
        print("Missing required environment variables:")
        for name in missing:
            print(f"- {name}")
        return

    url = f"{base_url.rstrip('/')}/messages"

    headers = {
        "x-api-key": api_key,
        "anthropic-version": anthropic_version,
        "content-type": "application/json",
    }

    safe_headers = {
        **headers,
        "x-api-key": mask_secret(api_key),
    }

    payload = {
        "model": model,
        "max_tokens": 1024,
        "messages": [
            {
                "role": "user",
                "content": "Привет, это проверка связи. Ответь одним словом 'ОК'.",
            }
        ],
    }

    print(f"Calling URL: {url}")
    print("Sending headers:")
    print(json.dumps(safe_headers, ensure_ascii=False, indent=2))
    print("Sending payload:")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"Request timeout: {TIMEOUT_SECONDS} seconds")

    started_at = time.perf_counter()

    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=TIMEOUT_SECONDS,
        )
        elapsed = time.perf_counter() - started_at
        print(f"Response received in {elapsed:.2f} seconds")
        print(f"HTTP status: {response.status_code}")

        response.raise_for_status()

        try:
            print("Response JSON:")
            print(json.dumps(response.json(), ensure_ascii=False, indent=2))
        except ValueError:
            print("Response text:")
            print(response.text)

    except requests.exceptions.Timeout as exc:
        elapsed = time.perf_counter() - started_at
        print(f"Timeout after {elapsed:.2f} seconds")
        print(f"Configured timeout was {TIMEOUT_SECONDS} seconds")
        print(f"Error: {exc}")

    except requests.exceptions.HTTPError as exc:
        elapsed = time.perf_counter() - started_at
        response = exc.response
        print(f"HTTP error after {elapsed:.2f} seconds")
        if response is not None:
            print(f"Status code: {response.status_code}")
            print("Error response body:")
            print(response.text)
        else:
            print("No response object attached to HTTPError")
        print(f"Error: {exc}")

    except Exception as exc:
        elapsed = time.perf_counter() - started_at
        print(f"Unexpected error after {elapsed:.2f} seconds")
        print(f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
