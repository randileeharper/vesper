from __future__ import annotations

import json

import httpx
import pytest

from vesper.config import Settings
from vesper.errors import ResolverError
from vesper.resolver import FallbackResolver, OpenAICompatibleResolver


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


def test_openai_compatible_resolver_retries_blank_content_until_success(settings: Settings, service) -> None:
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

    call_count = 0

    class RetryTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                body = {"choices": [{"message": {"content": ""}}]}
            else:
                body = {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps({"action": "status", "parameters": {}}),
                            }
                        }
                    ]
                }
            return httpx.Response(200, json=body)

    session = httpx.Client(base_url=resolver_settings.resolver_base_url, transport=RetryTransport())
    resolver = OpenAICompatibleResolver(resolver_settings, session=session)

    resolved = resolver.resolve("what's playing?", service)

    assert resolved.action == "status"
    assert call_count == 3


def test_openai_compatible_resolver_stops_after_five_bad_attempts(settings: Settings, service) -> None:
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

    call_count = 0

    class ExhaustedRetryTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            body = {"choices": [{"message": {"content": "nonsense with no json anywhere"}}]}
            return httpx.Response(200, json=body)

    session = httpx.Client(base_url=resolver_settings.resolver_base_url, transport=ExhaustedRetryTransport())
    resolver = OpenAICompatibleResolver(resolver_settings, session=session)

    with pytest.raises(ResolverError, match="Resolver failed after 5 attempts"):
        resolver.resolve("what's playing?", service)

    assert call_count == 5


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


def test_openai_compatible_resolver_enables_reasoning_fields_when_configured(settings: Settings, service) -> None:
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
    assert body["think"] is True
    assert body["reasoning_effort"] == "medium"
    assert body["reasoning"] == {"effort": "medium"}


def test_openai_compatible_resolver_uses_compact_allowed_action_list(settings: Settings, service) -> None:
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

    class CapturePromptTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content.decode("utf-8"))
            body = {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps({"action": "status", "parameters": {}}),
                        }
                    }
                ]
            }
            return httpx.Response(200, json=body)

    session = httpx.Client(base_url=resolver_settings.resolver_base_url, transport=CapturePromptTransport())
    resolver = OpenAICompatibleResolver(resolver_settings, session=session)

    resolver.resolve("what's in queue?", service)

    body = captured["body"]
    assert isinstance(body, dict)
    messages = body["messages"]
    assert isinstance(messages, list)
    user_prompt = messages[1]["content"]
    assert "allowed_actions" in user_prompt
    assert '"preferences"' not in user_prompt
    assert "move_queue_item" not in user_prompt
    assert "remove_queue_item" not in user_prompt
    assert "clear_queue" not in user_prompt
    assert "play_item(" not in user_prompt
    assert "steer_session(request[, search_update])" in user_prompt
    assert "like_current_track()" in user_prompt
    assert "set_volume(volume)" in user_prompt
    assert "list_library_playlists()" in user_prompt
    assert "play_library_playlist(playlist_name)" in user_prompt
    assert "recommend(" not in user_prompt


def test_openai_compatible_resolver_passes_through_search_query_verbatim(settings: Settings, service) -> None:
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

    assert resolved.parameters["query"] == "popular songs by Pink"


def test_openai_compatible_resolver_normalizes_common_action_aliases(settings: Settings, service) -> None:
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

    class AliasTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            body = {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "action": "now_playing",
                                    "parameters": {},
                                }
                            )
                        }
                    }
                ]
            }
            return httpx.Response(200, json=body)

    session = httpx.Client(base_url=resolver_settings.resolver_base_url, transport=AliasTransport())
    resolver = OpenAICompatibleResolver(resolver_settings, session=session)

    resolved = resolver.resolve("what's playing?", service)

    assert resolved.action == "get_now_playing"


def test_openai_compatible_resolver_strips_function_style_action_suffix(settings: Settings, service) -> None:
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

    class FunctionStyleTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            body = {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "action": "next_track()",
                                    "parameters": {},
                                }
                            )
                        }
                    }
                ]
            }
            return httpx.Response(200, json=body)

    session = httpx.Client(base_url=resolver_settings.resolver_base_url, transport=FunctionStyleTransport())
    resolver = OpenAICompatibleResolver(resolver_settings, session=session)

    resolved = resolver.resolve("play the next track", service)

    assert resolved.action == "next_track"


def test_openai_compatible_resolver_normalizes_structured_session_search_update(settings: Settings, service) -> None:
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

    class SearchUpdateTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            body = {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "action": "steer_session",
                                    "parameters": {
                                        "request": "add some radwimps",
                                            "search_update": {
                                                "mode": "add",
                                                "sources": [
                                                    {"kind": "artist", "term": " RADWIMPS "},
                                                    {"kind": "artist", "term": "RADWIMPS"},
                                                ],
                                            },
                                    },
                                }
                            )
                        }
                    }
                ]
            }
            return httpx.Response(200, json=body)

    session = httpx.Client(base_url=resolver_settings.resolver_base_url, transport=SearchUpdateTransport())
    resolver = OpenAICompatibleResolver(resolver_settings, session=session)

    resolved = resolver.resolve("add some radwimps", service)

    assert resolved.parameters["search_update"] == {
        "mode": "add",
        "sources": [{"kind": "artist", "term": "RADWIMPS"}],
    }


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


def test_openai_compatible_resolver_can_like_current_track(settings: Settings, service) -> None:
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

    class LikeTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            body = {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "action": "like_current_track",
                                    "parameters": {},
                                }
                            )
                        }
                    }
                ]
            }
            return httpx.Response(200, json=body)

    session = httpx.Client(base_url=resolver_settings.resolver_base_url, transport=LikeTransport())
    resolver = OpenAICompatibleResolver(resolver_settings, session=session)

    resolved = resolver.resolve("i like this track", service)

    assert resolved.action == "like_current_track"
    assert resolved.parameters == {}


def test_openai_compatible_resolver_can_list_playlists(settings: Settings, service) -> None:
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

    class PlaylistListTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            body = {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps({"action": "list_library_playlists", "parameters": {}}),
                        }
                    }
                ]
            }
            return httpx.Response(200, json=body)

    session = httpx.Client(base_url=resolver_settings.resolver_base_url, transport=PlaylistListTransport())
    resolver = OpenAICompatibleResolver(resolver_settings, session=session)

    resolved = resolver.resolve("what playlists are available?", service)

    assert resolved.action == "list_library_playlists"
    assert resolved.parameters == {}


def test_openai_compatible_resolver_can_play_playlist_by_name(settings: Settings, service) -> None:
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

    class PlaylistPlayTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            body = {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"action": "play_library_playlist", "parameters": {"playlist": "Mix"}}
                            ),
                        }
                    }
                ]
            }
            return httpx.Response(200, json=body)

    session = httpx.Client(base_url=resolver_settings.resolver_base_url, transport=PlaylistPlayTransport())
    resolver = OpenAICompatibleResolver(resolver_settings, session=session)

    resolved = resolver.resolve("play my playlist mix", service)

    assert resolved.action == "play_library_playlist"
    assert resolved.parameters == {"playlist_name": "Mix"}


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
    assert resolved.parameters == {
        "request": "more like Favorite Artist - Liked Song",
        "search_update": {"mode": "preserve", "sources": []},
    }


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
    assert "candidate_artists" not in resolved.parameters
    assert resolved.parameters["candidate_queries"] == ["popular songs by Pink"]


def test_artist_only_candidate_output_becomes_adaptive_session(settings: Settings, service) -> None:
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

    class ArtistTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            body = {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "action": "play_candidate_match",
                                    "parameters": {
                                        "candidate_artists": ["RADWIMPS"],
                                        "candidate_queries": ["RADWIMPS"],
                                    },
                                }
                            )
                        }
                    }
                ]
            }
            return httpx.Response(200, json=body)

    session = httpx.Client(base_url=resolver_settings.resolver_base_url, transport=ArtistTransport())
    resolver = OpenAICompatibleResolver(resolver_settings, session=session)

    resolved = resolver.resolve("play radwimps", service)

    assert resolved.action == "play_session"
    assert resolved.parameters == {"request": "RADWIMPS"}


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


def test_candidate_match_synthesizes_fallback_query_from_verbatim_request_text(settings: Settings, service) -> None:
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
    assert resolved.parameters["candidate_queries"] == ["play music for bedtime"]


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


def test_filter_session_queue_parses_eligible_indices_and_policy(settings: Settings, service) -> None:
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

    class QueueFilterTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            body = {"choices": [{"message": {"content": json.dumps({"eligible_indices": [1, 0, 9, 1], "queue_policy": "shuffle"})}}]}
            return httpx.Response(200, json=body)

    session = httpx.Client(base_url=resolver_settings.resolver_base_url, transport=QueueFilterTransport())
    resolver = OpenAICompatibleResolver(resolver_settings, session=session)

    decision = resolver.filter_session_queue(
        "more pop",
        service,
        {"request_text": "play upbeat music", "steering_history": ["more pop"]},
        [
            {"id": "a", "title": "One", "artist": "Artist A"},
            {"id": "b", "title": "Two", "artist": "Artist B"},
        ],
    )

    assert decision.eligible_indices == [1, 0]
    assert decision.queue_policy == "shuffle"


def test_session_plan_prompt_omits_recent_track_history(settings: Settings, service) -> None:
    captured_payload: dict[str, object] = {}

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

    class CapturePlanTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal captured_payload
            captured_payload = json.loads(request.content.decode("utf-8"))
            body = {"choices": [{"message": {"content": json.dumps({"search_queries": ["KATSEYE"]})}}]}
            return httpx.Response(200, json=body)

    session = httpx.Client(base_url=resolver_settings.resolver_base_url, transport=CapturePlanTransport())
    resolver = OpenAICompatibleResolver(resolver_settings, session=session)

    resolver.plan_session("play some KATSEYE", service, {"request_text": "play some KATSEYE"}, 1)

    prompt = captured_payload["messages"][1]["content"]
    assert '"session_request"' in prompt
    assert '"session_steering"' in prompt
    assert '"preferences"' not in prompt
    assert '"recent_tracks"' not in prompt
    assert '"global_recent_tracks"' not in prompt
    system_prompt = captured_payload["messages"][0]["content"]
    assert "preserve that request broadly" in system_prompt
    assert "play trip-hop" in system_prompt
    assert "Use more creative interpretation only when the request is open-ended" in system_prompt


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


def test_resolver_rejects_action_outside_allowlist(settings: Settings, service) -> None:
    """An advanced_only/internal action returned by the model must not execute.

    The resolver prompt advertises only RESOLVER_ACTION_NAMES. If the model
    (benignly or via prompt injection through catalog text) returns an action
    like play_item that exists in ACTION_REGISTRY but is not resolver-selectable,
    resolution must fail rather than pass the action through to execute_action.
    """
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

    class InjectedActionTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            body = {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "action": "play_item",
                                    "parameters": {"item_id": "1440818839"},
                                }
                            )
                        }
                    }
                ]
            }
            return httpx.Response(200, json=body)

    session = httpx.Client(base_url=resolver_settings.resolver_base_url, transport=InjectedActionTransport())
    resolver = OpenAICompatibleResolver(resolver_settings, session=session)

    with pytest.raises(ResolverError):
        resolver.resolve("play a song", service)
