from dataclasses import dataclass


@dataclass
class SceneUnderstanding:
    scene_index: int
    start_timestamp: float
    end_timestamp: float
    visual_summary: str
    key_actions:str
    mood:str
