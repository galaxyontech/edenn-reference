from __future__ import annotations

import os
from pathlib import Path

from EdennCode.ModelFactory.VideoSFXModelFactory.CloudSoundEffectGen.provider_a_sound_effect import (
    ProviderASoundEffectProvider,
)


def build_sound_effect_provider() -> ProviderASoundEffectProvider:
    timeout = int(os.getenv("PROVIDER_A_SFX_TIMEOUT", "60"))
    endpoint = (os.getenv("PROVIDER_A_SFX_ENDPOINT", "") or None)
    model_id = (os.getenv("PROVIDER_A_SFX_MODEL_ID", "") or None)
    output_format = (os.getenv("PROVIDER_A_SFX_OUTPUT_FORMAT", "") or None)
    _ = Path(os.getenv("OUTPUT_AUDIO_DIR", "outputs/audio"))
    return ProviderASoundEffectProvider(
        endpoint=endpoint,
        model_id=model_id,
        output_format=output_format,
        timeout=timeout,
    )
