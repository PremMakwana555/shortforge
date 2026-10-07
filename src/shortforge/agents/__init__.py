from ..core.worker import Agent
from ..models import Stage
from .editing import EditingAgent
from .publishing import PublishingAgent
from .qa import QAAgent
from .research import ResearchAgent
from .script import ScriptAgent
from .visual import VisualAgent
from .voiceover import VoiceoverAgent


def all_agents() -> dict[Stage, Agent]:
    agents: list[Agent] = [ResearchAgent(), ScriptAgent(), VoiceoverAgent(), VisualAgent(), EditingAgent(),
                           QAAgent(), PublishingAgent()]
    return {a.stage: a for a in agents}


__all__ = ["all_agents"]
