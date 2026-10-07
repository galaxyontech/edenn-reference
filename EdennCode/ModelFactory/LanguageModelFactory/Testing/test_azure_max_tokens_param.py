import unittest

import httpx
from openai import BadRequestError

from EdennCode.ModelFactory.LanguageModelFactory.azure_based_model_model_gateway import (
    AzureMultimodalClient,
    _uses_max_completion_tokens,
)


def _make_client(model: str) -> AzureMultimodalClient:
    return AzureMultimodalClient(
        azure_endpoint="https://example.cognitiveservices.azure.com/",
        azure_api_version="2024-12-01-preview",
        azure_model=model,
        api_key="test-key",
        api_mode="chat_completions",
    )


def _max_tokens_bad_request() -> BadRequestError:
    body = {
        "error": {
            "message": (
                "Unsupported parameter: 'max_tokens' is not supported with this "
                "model. Use 'max_completion_tokens' instead."
            ),
            "type": "invalid_request_error",
            "param": "max_tokens",
            "code": "unsupported_parameter",
        }
    }
    request = httpx.Request("POST", "https://example.test")
    response = httpx.Response(400, request=request, json=body)
    return BadRequestError("max_tokens unsupported", response=response, body=body["error"])


class _FakeChatResponse:
    def __init__(self, content: str) -> None:
        message = type("_Msg", (), {"content": content})()
        self.choices = [type("_Choice", (), {"message": message})()]
        self.usage = None


class _FakeCreate:
    """Stand-in for client.chat.completions.create that records call kwargs."""

    def __init__(self, *, reject_max_tokens: bool) -> None:
        self.reject_max_tokens = reject_max_tokens
        self.calls: list[dict] = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.reject_max_tokens and "max_tokens" in kwargs:
            raise _max_tokens_bad_request()
        return _FakeChatResponse('{"ok": true}')


class _FakeClient:
    def __init__(self, create) -> None:
        self.chat = type(
            "_Chat", (), {"completions": type("_Comp", (), {"create": create})()}
        )()


class MaxTokensParamTests(unittest.IsolatedAsyncioTestCase):
    def test_detection_matches_reasoning_models(self) -> None:
        self.assertTrue(_uses_max_completion_tokens("chat-standard"))
        self.assertTrue(_uses_max_completion_tokens("o3-mini"))
        self.assertTrue(_uses_max_completion_tokens("my-chat-standard-deploy"))
        self.assertFalse(_uses_max_completion_tokens("chat-advanced"))
        self.assertFalse(_uses_max_completion_tokens("chat-legacy"))

    async def test_gpt5_uses_max_completion_tokens_directly(self) -> None:
        client = _make_client("chat-standard")
        create = _FakeCreate(reject_max_tokens=True)
        client.client = _FakeClient(create)

        payload, _ = await client.complete_messages([], json_schema={}, max_tokens=123)

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(len(create.calls), 1)
        self.assertEqual(create.calls[0]["max_completion_tokens"], 123)
        self.assertNotIn("max_tokens", create.calls[0])

    async def test_unknown_deployment_falls_back_after_400(self) -> None:
        # Deployment name doesn't trip detection, but the provider still rejects
        # max_tokens -> we swap the param and retry once.
        client = _make_client("prod-alias")
        create = _FakeCreate(reject_max_tokens=True)
        client.client = _FakeClient(create)

        payload, _ = await client.complete_messages([], json_schema={}, max_tokens=77)

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(len(create.calls), 2)
        self.assertIn("max_tokens", create.calls[0])
        self.assertEqual(create.calls[1]["max_completion_tokens"], 77)
        self.assertNotIn("max_tokens", create.calls[1])


if __name__ == "__main__":
    unittest.main()
