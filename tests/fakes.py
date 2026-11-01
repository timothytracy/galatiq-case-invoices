"""The model double shared by the pipeline tests. No network."""


class ZeroRandom:
    def uniform(self, _start, _end):
        return 0.0


class FakeClock:
    def __init__(self):
        self.slept = []

    def sleep(self, seconds):
        self.slept.append(seconds)


class FakeLLM:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def request(self, messages, timeout=60):
        self.calls.append(messages)
        result = self.handler(messages)
        if isinstance(result, Exception):
            raise result
        return result


def task_of(messages) -> str:
    for message in messages:
        first = (message.get("content") or "").splitlines()[0]
        if first.startswith("TASK "):
            return first.split()[1]
    return ""


class ScriptedToolModel:
    """A queue of replies. Each step is {"content", "tool_calls"} or an exception.

    request() is the plain path. request_tools() is the tools path.
    Existing tests keep using FakeLLM.
    """

    def __init__(self, steps):
        self.steps = list(steps)
        self.requests = []
        self.tool_requests = []

    def request(self, messages, timeout=60):
        self.requests.append({"messages": messages, "timeout": timeout})
        step = self._take()
        if isinstance(step, Exception):
            raise step
        if step.get("tool_calls"):
            raise AssertionError("plain request received tool_calls")
        return step.get("content") or ""

    def request_tools(self, messages, tools, timeout=60):
        self.tool_requests.append({"messages": messages, "tools": tools, "timeout": timeout})
        step = self._take()
        if isinstance(step, Exception):
            raise step
        return step

    def _take(self):
        if not self.steps:
            raise AssertionError("scripted model ran out of steps")
        return self.steps.pop(0)
