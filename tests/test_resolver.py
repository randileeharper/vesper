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
        resolver_include_reasoning=False,
        resolver_include_raw_output=False,
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
        resolver_include_reasoning=False,
        resolver_include_raw_output=False,
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


def test_openai_compatible_resolver_includes_reasoning_when_enabled(settings: Settings, service) -> None:
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
        resolver_include_reasoning=True,
        resolver_include_raw_output=False,
        request_timeout_seconds=settings.request_timeout_seconds,
        verify_tls=settings.verify_tls,
        log_level=settings.log_level,
        database_path=settings.database_path,
        config_path=settings.config_path,
    )

    class ReasoningTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            body = {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps({"action": "status", "parameters": {}}),
                            "reasoning": "I mapped the request to a simple status action.",
                        }
                    }
                ]
            }
            return httpx.Response(200, json=body)

    session = httpx.Client(base_url=resolver_settings.resolver_base_url, transport=ReasoningTransport())
    resolver = OpenAICompatibleResolver(resolver_settings, session=session)

    resolved = resolver.resolve("play some kep1er", service)

    assert resolved.reasoning == "I mapped the request to a simple status action."


def test_openai_compatible_resolver_can_reject_current_track(settings: Settings, service) -> None:
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
        resolver_include_reasoning=False,
        resolver_include_raw_output=False,
        request_timeout_seconds=settings.request_timeout_seconds,
        verify_tls=settings.verify_tls,
        log_level=settings.log_level,
        database_path=settings.database_path,
        config_path=settings.config_path,
    )

    class RejectTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            body = {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "action": "reject_current_track",
                                    "parameters": {},
                                }
                            )
                        }
                    }
                ]
            }
            return httpx.Response(200, json=body)

    session = httpx.Client(base_url=resolver_settings.resolver_base_url, transport=RejectTransport())
    resolver = OpenAICompatibleResolver(resolver_settings, session=session)

    resolved = resolver.resolve("i don't like this", service)

    assert resolved.action == "reject_current_track"
    assert resolved.parameters == {}


def test_candidate_match_parameters_are_normalized(settings: Settings, service) -> None:
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
        resolver_include_reasoning=False,
        resolver_include_raw_output=False,
        request_timeout_seconds=settings.request_timeout_seconds,
        verify_tls=settings.verify_tls,
        log_level=settings.log_level,
        database_path=settings.database_path,
        config_path=settings.config_path,
    )

    class CandidateTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            body = {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "action": "play_candidate_match",
                                    "parameters": {
                                        "candidate_tracks": [{"title": "Just Give Me a Reason", "artist": "P!nk"}],
                                        "candidate_artists": ["P!nk"],
                                        "candidate_queries": ["popular songs by Pink"],
                                    },
                                }
                            )
                        }
                    }
                ]
            }
            return httpx.Response(200, json=body)

    session = httpx.Client(base_url=resolver_settings.resolver_base_url, transport=CandidateTransport())
    resolver = OpenAICompatibleResolver(resolver_settings, session=session)

    resolved = resolver.resolve("play some pink", service)

    assert resolved.action == "play_candidate_match"
    assert resolved.parameters["candidate_artists"] == ["P!nk"]
    assert resolved.parameters["candidate_queries"] == ["Pink"]


def test_candidate_match_singular_query_alias_is_normalized(settings: Settings, service) -> None:
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
        resolver_include_reasoning=False,
        request_timeout_seconds=settings.request_timeout_seconds,
        verify_tls=settings.verify_tls,
        log_level=settings.log_level,
        database_path=settings.database_path,
        config_path=settings.config_path,
    )

    class CandidateAliasTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            body = {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "action": "play_candidate_match",
                                    "parameters": {
                                        "candidate_tracks": [{"title": "Weightless", "artist": "Marconi Union"}],
                                        "candidate_query": "relaxing sleep music",
                                    },
                                }
                            )
                        }
                    }
                ]
            }
            return httpx.Response(200, json=body)

    session = httpx.Client(base_url=resolver_settings.resolver_base_url, transport=CandidateAliasTransport())
    resolver = OpenAICompatibleResolver(resolver_settings, session=session)

    resolved = resolver.resolve("play music to make me sleepy", service)

    assert resolved.action == "play_candidate_match"
    assert resolved.parameters["candidate_tracks"] == [{"title": "Weightless", "artist": "Marconi Union"}]
    assert resolved.parameters["candidate_queries"] == ["relaxing sleep music"]
    assert "candidate_query" not in resolved.parameters


def test_candidate_match_synthesizes_fallback_query_from_request_text(settings: Settings, service) -> None:
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
        resolver_include_reasoning=False,
        resolver_include_raw_output=False,
        request_timeout_seconds=settings.request_timeout_seconds,
        verify_tls=settings.verify_tls,
        log_level=settings.log_level,
        database_path=settings.database_path,
        config_path=settings.config_path,
    )

    class CandidateFallbackTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            body = {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "action": "play_candidate_match",
                                    "parameters": {
                                        "candidate_tracks": [{"title": "Sleep", "artist": "Lofi Girl"}],
                                    },
                                }
                            )
                        }
                    }
                ]
            }
            return httpx.Response(200, json=body)

    session = httpx.Client(base_url=resolver_settings.resolver_base_url, transport=CandidateFallbackTransport())
    resolver = OpenAICompatibleResolver(resolver_settings, session=session)

    resolved = resolver.resolve("play music for bedtime", service)

    assert resolved.action == "play_candidate_match"
    assert resolved.parameters["candidate_queries"] == ["bedtime music"]


def test_openai_compatible_resolver_includes_raw_output_when_enabled(settings: Settings, service) -> None:
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
        resolver_include_reasoning=False,
        resolver_include_raw_output=True,
        request_timeout_seconds=settings.request_timeout_seconds,
        verify_tls=settings.verify_tls,
        log_level=settings.log_level,
        database_path=settings.database_path,
        config_path=settings.config_path,
    )

    raw_content = json.dumps({"action": "status", "parameters": {}})

    class RawOutputTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            body = {
                "choices": [
                    {
                        "message": {
                            "content": raw_content,
                        }
                    }
                ]
            }
            return httpx.Response(200, json=body)

    session = httpx.Client(base_url=resolver_settings.resolver_base_url, transport=RawOutputTransport())
    resolver = OpenAICompatibleResolver(resolver_settings, session=session)

    resolved = resolver.resolve("what's playing?", service)

    assert resolved.raw_content == raw_content
    assert resolved.raw == {"action": "status", "parameters": {}}


def test_session_plan_prefers_live_artist_selection_for_artist_request(settings: Settings, service) -> None:
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
        resolver_include_reasoning=False,
        resolver_include_raw_output=False,
        request_timeout_seconds=settings.request_timeout_seconds,
        verify_tls=settings.verify_tls,
        log_level=settings.log_level,
        database_path=settings.database_path,
        config_path=settings.config_path,
    )

    class ArtistSessionTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            body = {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "candidate_tracks": [{"title": "Old Training Data Song", "artist": "KATSEYE"}],
                                    "candidate_artists": [],
                                    "candidate_queries": [],
                                }
                            )
                        }
                    }
                ]
            }
            return httpx.Response(200, json=body)

    session = httpx.Client(base_url=resolver_settings.resolver_base_url, transport=ArtistSessionTransport())
    resolver = OpenAICompatibleResolver(resolver_settings, session=session)

    plan = resolver.plan_session("play some KATSEYE", service, {"request_text": "play some KATSEYE"}, 3)

    assert plan.candidate_tracks == []
    assert plan.candidate_artists[0] == "KATSEYE"
    assert plan.candidate_queries == ["KATSEYE"]


def test_play_session_request_text_is_normalized_to_request(settings: Settings, service) -> None:
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
        resolver_include_reasoning=False,
        resolver_include_raw_output=False,
        request_timeout_seconds=settings.request_timeout_seconds,
        verify_tls=settings.verify_tls,
        log_level=settings.log_level,
        database_path=settings.database_path,
        config_path=settings.config_path,
    )

    class RequestTextTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            body = {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "action": "play_session",
                                    "parameters": {
                                        "request_text": "upbeat and energetic music for cleaning the house",
                                    },
                                }
                            )
                        }
                    }
                ]
            }
            return httpx.Response(200, json=body)

    session = httpx.Client(base_url=resolver_settings.resolver_base_url, transport=RequestTextTransport())
    resolver = OpenAICompatibleResolver(resolver_settings, session=session)

    resolved = resolver.resolve("my mom is visiting this morning so i have to clean. help!", service)

    assert resolved.action == "play_session"
    assert resolved.parameters["request"] == "upbeat and energetic music for cleaning the house"
    assert "request_text" not in resolved.parameters
