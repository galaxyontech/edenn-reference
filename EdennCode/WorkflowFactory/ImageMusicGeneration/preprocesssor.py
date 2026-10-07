from __future__ import annotations

"""
Backward-compatible exports for the image-to-music workflow.
"""

from EdennCode.ModelFactory.LanguageModelFactory.azure_based_model_model_gateway import AzureMultimodalClient
from EdennCode.WorkflowFactory.ImageMusicGeneration.workflow import ImageMusicWorkflow, MusicGenResult

# Preserve the previous name while delegating to the new workflow class.
ImageMusicPreprocessor = ImageMusicWorkflow

__all__ = ["AzureMultimodalClient", "ImageMusicPreprocessor", "ImageMusicWorkflow", "MusicGenResult"]
