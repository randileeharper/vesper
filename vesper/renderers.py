"""Thin transport renderers for Vesper."""

from __future__ import annotations

from typing import Any

from google.protobuf.json_format import MessageToDict

from .results import EngineActionResult, TextRequestResult


def render_text_result_for_a2a(result: TextRequestResult) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = result.execution.result if isinstance(result.execution.result, dict) else {"value": result.execution.result}
    metadata: dict[str, Any] = {
        "action": result.execution.action,
        "summary": result.summary or "",
        "resolver": result.resolver,
        "resolved_action": result.resolved_action,
    }
    if result.reasoning:
        metadata["reasoning"] = result.reasoning
    if result.resolver_raw_content:
        metadata["resolver_raw_content"] = result.resolver_raw_content
    if result.resolver_raw_action is not None:
        metadata["resolver_raw_action"] = result.resolver_raw_action
    if result.timings is not None:
        metadata["timings"] = result.timings
    return payload, metadata


def render_action_result_for_a2a(result: EngineActionResult) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = result.result if isinstance(result.result, dict) else {"value": result.result}
    return payload, {
        "action": result.action,
        "summary": str(payload.get("summary", "")).strip() if isinstance(payload, dict) else "",
    }


def render_task_payload_for_cli(task: Any, *, original_text: str | None = None) -> dict[str, Any]:
    payload = _extract_payload(task)
    metadata = _extract_metadata(task)
    action = metadata.get("action")
    if original_text is None:
        return payload
    response: dict[str, Any] = {
        "status": "ok",
        "input": original_text,
        "resolver": metadata.get("resolver"),
        "resolved_action": metadata.get("resolved_action", {"action": action} if action else {}),
        "execution": {
            "action": action,
            "result": payload,
        },
    }
    if metadata.get("summary"):
        response["summary"] = metadata["summary"]
    if "reasoning" in metadata:
        response["reasoning"] = metadata["reasoning"]
    if "resolver_raw_content" in metadata:
        response["resolver_raw_content"] = metadata["resolver_raw_content"]
    if "resolver_raw_action" in metadata:
        response["resolver_raw_action"] = metadata["resolver_raw_action"]
    if "timings" in metadata:
        response["timings"] = metadata["timings"]
    return response


def _extract_data_part(parts: Any) -> dict[str, Any] | None:
    if not isinstance(parts, list):
        return None
    for part in parts:
        if isinstance(part, dict) and isinstance(part.get("data"), dict):
            return dict(part["data"])
    return None


def _proto_to_dict(message: Any) -> dict[str, Any]:
    return MessageToDict(message, preserving_proto_field_name=False)


def _extract_metadata(result: Any) -> dict[str, Any]:
    if _is_proto_message(result):
        payload = _proto_to_dict(result)
    else:
        payload = result
    metadata = payload.get("metadata", {})
    return dict(metadata) if isinstance(metadata, dict) else {}


def _extract_payload(result: Any) -> dict[str, Any]:
    if _looks_like_message(result):
        payload = _extract_data_part(_proto_to_dict(result).get("parts", []))
        if payload is not None:
            return payload
        raise ValueError("Message did not include a data part.")

    if _looks_like_task(result):
        return _extract_task_payload_dict(_proto_to_dict(result))

    return _extract_task_payload_dict(result)


def _extract_task_payload_dict(task: dict[str, Any]) -> dict[str, Any]:
    artifacts = task.get("artifacts", [])
    if isinstance(artifacts, list):
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            payload = _extract_data_part(artifact.get("parts", []))
            if payload is not None:
                return payload

    status = task.get("status", {})
    if isinstance(status, dict):
        message = status.get("message", {})
        if isinstance(message, dict):
            payload = _extract_data_part(message.get("parts", []))
            if payload is not None:
                return payload

    raise ValueError("Task did not include a data artifact.")


def _is_proto_message(value: Any) -> bool:
    return hasattr(value, "DESCRIPTOR")


def _looks_like_message(value: Any) -> bool:
    return _is_proto_message(value) and hasattr(value, "parts") and hasattr(value, "role")


def _looks_like_task(value: Any) -> bool:
    return _is_proto_message(value) and hasattr(value, "status") and hasattr(value, "artifacts")
