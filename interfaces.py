"""What the stages need from the model client, the clock, and the random source.

GrokClient, SleepClock, and random.Random satisfy these. So do the test doubles.
"""

from typing import Protocol


class LLM(Protocol):
    def request(self, messages: list[dict], timeout: float = 60) -> str: ...

    def request_tools(self, messages: list[dict], tools: list[dict], timeout: float = 60) -> dict: ...


class Clock(Protocol):
    def sleep(self, seconds: float) -> None: ...


class Rng(Protocol):
    def uniform(self, a: float, b: float) -> float: ...
