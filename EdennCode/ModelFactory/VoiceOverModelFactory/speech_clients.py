"""The boundary where the hosted-speech SDK is imported.

Same contract as the language-model adapter: the vendor package cannot be
renamed, so it is imported here and nowhere else, and callers ask for a client
by role. See ``EdennCode/ModelFactory/LanguageModelFactory/gateway_clients.py``.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from elevenlabs.client import ElevenLabs as _HostedSpeechClient

__all__ = ["HostedSpeechClient", "make_hosted_speech_client"]

HostedSpeechClient = _HostedSpeechClient


def make_hosted_speech_client(api_key: Optional[str] = None) -> Any:
    """A client for the hosted speech-synthesis provider."""
    return _HostedSpeechClient(api_key=api_key or os.getenv("PROVIDER_A_API_KEY", ""))
