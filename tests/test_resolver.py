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


def test_openai_compatible_resolver_sends_no_think_ollama_fields(settings: Settings, service) -> None:
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

    captured: dict[str, object] = {}

    class CaptureTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content.decode("utf-8"))
            body = {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "action": "status",
                                    "parameters": {},
                                }
                            )
                        }
                    }
                ]
            }
            return httpx.Response(200, json=body)

    session = httpx.Client(base_url=resolver_settings.resolver_base_url, transport=CaptureTransport())
    resolver = OpenAICompatibleResolver(resolver_settings, session=session)

    resolver.resolve("play some kep1er", service)

    body = captured["body"]
    assert isinstance(body, dict)
    assert body["think"] is False
    assert body["reasoning_effort"] == "none"
    assert body["reasoning"] == {"effort": "none"}


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


def test_openai_compatible_resolver_includes_structured_reasoning_when_enabled(settings: Settings, service) -> None:
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

    class StructuredReasoningTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            body = {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps({"action": "status", "parameters": {}}),
                            "reasoning_content": [
                                {"text": "First thought."},
                                {"text": "Second thought."},
                            ],
                        }
                    }
                ]
            }
            return httpx.Response(200, json=body)

    session = httpx.Client(base_url=resolver_settings.resolver_base_url, transport=StructuredReasoningTransport())
    resolver = OpenAICompatibleResolver(resolver_settings, session=session)

    resolved = resolver.resolve("play some kep1er", service)

    assert resolved.reasoning == "First thought.\nSecond thought."


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


def test_openai_compatible_resolver_can_steer_session_without_interrupting(settings: Settings, service) -> None:
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

    class SteerTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            body = {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "action": "steer_session",
                                    "parameters": {"request": "more like Favorite Artist - Liked Song"},
                                }
                            )
                        }
                    }
                ]
            }
            return httpx.Response(200, json=body)

    session = httpx.Client(base_url=resolver_settings.resolver_base_url, transport=SteerTransport())
    resolver = OpenAICompatibleResolver(resolver_settings, session=session)

    resolved = resolver.resolve("i like this artist", service)

    assert resolved.action == "steer_session"
    assert resolved.parameters == {"request": "more like Favorite Artist - Liked Song"}


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


def test_session_plan_does_not_override_model_output_with_artist_heuristics(settings: Settings, service) -> None:
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
                                        "search_queries": ["KATSEYE"],
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

    assert plan.search_queries == ["KATSEYE"]


def test_session_plan_caps_candidate_lists(settings: Settings, service) -> None:
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

    class CappedPlanTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            body = {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "search_queries": ["query one", "query two"],
                                }
                            )
                        }
                    }
                ]
            }
            return httpx.Response(200, json=body)

    session = httpx.Client(base_url=resolver_settings.resolver_base_url, transport=CappedPlanTransport())
    resolver = OpenAICompatibleResolver(resolver_settings, session=session)

    plan = resolver.plan_session("play cleaning music", service, {"request_text": "play cleaning music"}, 3)

    assert plan.search_queries == ["query one"]


def test_session_plan_uses_model_query_for_vibes_text(settings: Settings, service) -> None:
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

    class VibesTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            body = {
                "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "search_queries": ["cinematic piano music with emotive melodies"],
                                    }
                                )
                            }
                        }
                ]
            }
            return httpx.Response(200, json=body)

    session = httpx.Client(base_url=resolver_settings.resolver_base_url, transport=VibesTransport())
    resolver = OpenAICompatibleResolver(resolver_settings, session=session)

    plan = resolver.plan_session("piano music with radwimps vibes", service, {"request_text": "piano music with radwimps vibes"}, 3)

    assert plan.search_queries == ["cinematic piano music with emotive melodies"]


def test_select_session_track_returns_index_from_real_candidates(settings: Settings, service) -> None:
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

    class SelectTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            body = {"choices": [{"message": {"content": json.dumps({"selected_index": 1})}}]}
            return httpx.Response(200, json=body)

    session = httpx.Client(base_url=resolver_settings.resolver_base_url, transport=SelectTransport())
    resolver = OpenAICompatibleResolver(resolver_settings, session=session)

    selection = resolver.select_session_track(
        "anime piano music",
        service,
        {"request_text": "anime piano music"},
        "anime piano music",
        [
            {"id": "a", "title": "One", "artist": "Artist A"},
            {"id": "b", "title": "Two", "artist": "Artist B"},
        ],
    )

    assert selection.selected_index == 1


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
