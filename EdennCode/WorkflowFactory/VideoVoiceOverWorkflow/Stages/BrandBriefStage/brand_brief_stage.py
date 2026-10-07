from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from typing import Dict 





@dataclass(slots=True)
class BrandBriefStageInput:
    """
    Placeholder for future brand metadata injection.
    """
    agentic_information_payload: Dict[str,str] # for agentic informations of the brands 
    campaign_name: Optional[str] = None 
    special_offer: Optional[str] = None


@dataclass(slots=True)
class BrandBriefStageOutput:
    brand_brief: str


class BrandBriefStage:
    """
    For now, returns a static brand brief that can be swapped later.
    """

    def run(self, stage_input: BrandBriefStageInput) -> BrandBriefStageOutput:  # noqa: ARG002
        return BrandBriefStageOutput(brand_brief="")
