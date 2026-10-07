from __future__ import annotations

from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.cloud_music_gen_util import (
    build_edenn_enhanced_music_provider,
    build_music_provider,
)
from EdennCode.ModelFactory.MusicGenModelFactory.CloudMusicGen.provider_c import ProviderCApi

from .models import MusicModelSpec
from .providers.provider_a import ProviderAMusicGenerationStrategy
from .providers.provider_b import ProviderBMusicGenerationStrategy
from .providers.provider_c import ProviderCMusicGenerationStrategy
from .service import MusicGenerationService


def build_default_music_generation_service() -> MusicGenerationService:
    return MusicGenerationService(
        strategy_builders={
            MusicModelSpec.EDENN_BASIC: lambda: ProviderAMusicGenerationStrategy(
                music_provider=build_music_provider()
            ),
            MusicModelSpec.EDENN_ENHANCED: lambda: ProviderBMusicGenerationStrategy(
                music_provider=build_edenn_enhanced_music_provider()
            ),
            MusicModelSpec.EDENN_STUDIO: lambda: ProviderCMusicGenerationStrategy(
                music_provider=ProviderCApi()
            ),
        }
    )
