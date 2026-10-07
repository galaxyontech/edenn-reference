from __future__ import annotations

"""
Compatibility shim that surfaces the shared image-to-music workflow classes.
"""

from EdennCode.ModelFactory.LanguageModelFactory.azure_based_model_model_gateway import AzureMultimodalClient
from EdennCode.WorkflowFactory.ImageMusicGeneration.workflow import ImageMusicWorkflow, MusicGenResult

# Backward-compatible alias
ImageMusicPreprocessor = ImageMusicWorkflow

__all__ = ["AzureMultimodalClient", "ImageMusicWorkflow", "ImageMusicPreprocessor", "MusicGenResult"]
