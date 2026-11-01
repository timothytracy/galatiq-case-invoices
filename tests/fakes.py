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
