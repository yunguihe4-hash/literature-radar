"""Synthetic in-memory HTTP responses only; no live requests."""
import json
from literature_radar.sources.http import HTTPClient, HTTPResponse


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        if not self.responses:
            raise AssertionError("unexpected HTTP call")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, HTTPResponse):
            return item
        return HTTPResponse(200, {}, json.dumps(item).encode("utf-8"))


def client(provider_id, responses, retries=0):
    fake = FakeTransport(responses)
    return HTTPClient(provider_id, transport=fake, sleeper=lambda _: None,
                      max_retries=retries, rate_per_second=100000), fake
