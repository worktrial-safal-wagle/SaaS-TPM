from sim.npc.brain import (
    BrainCache,
    NpcBrain,
    NpcBrainContext,
    NpcBrainOutput,
    NpcTrigger,
    StubBrain,
)
from sim.npc.policy import NpcPolicy
from sim.npc.runtime import NpcRuntime

__all__ = [
    "AnthropicNPCBrain",
    "BrainCache",
    "NpcBrain",
    "NpcBrainContext",
    "NpcBrainOutput",
    "NpcPolicy",
    "NpcRuntime",
    "NpcTrigger",
    "StubBrain",
]


def __getattr__(name: str):
    # Lazy import — only pulls anthropic SDK when actually needed.
    if name == "AnthropicNPCBrain":
        from sim.npc.anthropic_brain import AnthropicNPCBrain
        return AnthropicNPCBrain
    raise AttributeError(name)
