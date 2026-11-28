"""How many tokens the model has used: for the latest message and for the whole session.

The counts are what the provider reported for each model call that finished. A call that fails reports nothing.
"""

from dataclasses import dataclass
from typing import Any


@dataclass
class Count:
    calls: int = 0
    input: int = 0  # sent to the model, including the cached part
    cached: int = 0  # the part of the input the provider had cached
    output: int = 0  # written by the model, including any reasoning

    def add(self, usage: Any) -> None:
        """Add one model call's usage (the SDK's `Usage`)."""
        self.calls += 1
        self.input += usage.input_tokens or 0
        self.cached += getattr(usage.input_tokens_details, "cached_tokens", 0) or 0
        self.output += usage.output_tokens or 0


class TokenMeter:
    def __init__(self) -> None:
        self.turn = Count()
        self.session = Count()
        self.last_input = 0  # what the latest call sent: how big the prompt, tools and history have grown

    def start_turn(self) -> None:
        self.turn = Count()

    def add(self, usage: Any) -> None:
        self.turn.add(usage)
        self.session.add(usage)
        self.last_input = usage.input_tokens or 0
