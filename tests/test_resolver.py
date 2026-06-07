from __future__ import annotations

import json

import httpx

from cider_agent.config import Settings
from cider_agent.resolver import FallbackResolver, OpenAICompatibleResolver


class FakeResolverTransport(httpx.BaseTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "action": "play_search_result",
                                "parameters": {"query": "kep1er", "source": "default", "index": 0},
                            }
                        )
                    }
                }
            ]
        }
        return httpx.Response(200, json=body)


def test_fallback_resolver_handles_simple_play(service) -> None:
    resolved = FallbackResolver().resolve("play", service)

    assert resolved.action == "play"
    assert resolved.parameters == {}


def test_openai_compatible_resolver_parses_action(settings: Settings, service) -> None:
    resolver_settings = Settings(
        http_host=settings.http_host,
        http_port=settings.http_port,
        public_base_url=settings.public_base_url,
        cider_base_url=settings.cider_base_url,
        cider_api_token=settings.cider_api_token,
        default_search_source=settings.default_search_source,
        resolver_backend="openai_compatible",
        resolver_base_url="https://resolver.example/v1",
        resolver_model="gpt-test",
        resolver_api_key="secret",
        request_timeout_seconds=settings.request_timeout_seconds,
        verify_tls=settings.verify_tls,
        log_level=settings.log_level,
        database_path=settings.database_path,
        config_path=settings.config_path,
    )
    session = httpx.Client(base_url=resolver_settings.resolver_base_url, transport=FakeResolverTransport())
    resolver = OpenAICompatibleResolver(resolver_settings, session=session)

    resolved = resolver.resolve("play some kep1er", service)

    assert resolved.action == "play_search_result"
    assert resolved.parameters["source"] == "default"


def test_openai_compatible_resolver_normalizes_descriptive_query(settings: Settings, service) -> None:
    resolver_settings = Settings(
        http_host=settings.http_host,
        http_port=settings.http_port,
        public_base_url=settings.public_base_url,
        cider_base_url=settings.cider_base_url,
        cider_api_token=settings.cider_api_token,
        default_search_source=settings.default_search_source,
        resolver_backend="openai_compatible",
        resolver_base_url="https://resolver.example/v1",
        resolver_model="gpt-test",
        resolver_api_key="secret",
        request_timeout_seconds=settings.request_timeout_seconds,
        verify_tls=settings.verify_tls,
        log_level=settings.log_level,
        database_path=settings.database_path,
        config_path=settings.config_path,
    )

    class DescriptiveTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            body = {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "action": "play_search_result",
                                    "parameters": {"query": "popular songs by Pink", "index": 0},
                                }
                            )
                        }
                    }
                ]
            }
            return httpx.Response(200, json=body)

    session = httpx.Client(base_url=resolver_settings.resolver_base_url, transport=DescriptiveTransport())
    resolver = OpenAICompatibleResolver(resolver_settings, session=session)

    resolved = resolver.resolve("play some pink", service)

    assert resolved.parameters["query"] == "Pink"
