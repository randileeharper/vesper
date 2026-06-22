"""Characterization tests for the input-validation helpers in ``vesper.validation``.

These pin the exact behavior of the validators and the volume/track-ref
coercers. The expected values were first captured against the original methods
on ``CiderAgentService`` and are unchanged after the extraction into
``vesper/validation.py`` - only the call target moved from ``self._<helper>``
to a module function - so a green run here proves the extraction is
behavior-preserving.
"""

from __future__ import annotations

import pytest

from vesper.errors import CiderValidationError
from vesper.validation import (
    coerce_volume_param,
    validate_index,
    validate_limit_offset,
    validate_playlist_id,
    validate_search,
)


# --- validate_search ------------------------------------------------------


def test_validate_search_rejects_empty_query():
    with pytest.raises(CiderValidationError, match="query cannot be empty."):
        validate_search("   ", 10)


def test_validate_search_rejects_bad_limit():
    with pytest.raises(CiderValidationError, match="limit must be between 1 and 100."):
        validate_search("q", 0)
    with pytest.raises(CiderValidationError, match="limit must be between 1 and 100."):
        validate_search("q", 101)


def test_validate_search_accepts_valid_bounds():
    assert validate_search("q", 1) is None
    assert validate_search("q", 100) is None


# --- validate_index -------------------------------------------------------


def test_validate_index_rejects_negative():
    with pytest.raises(CiderValidationError, match="from_index must be non-negative."):
        validate_index(-1, "from_index")


def test_validate_index_accepts_non_negative():
    assert validate_index(0, "index") is None
    assert validate_index(5, "to_index") is None


# --- validate_limit_offset -----------------------------------------------


def test_validate_limit_offset_rejects_bad_limit():
    with pytest.raises(CiderValidationError, match="limit must be between 1 and 100."):
        validate_limit_offset(0, 0)
    with pytest.raises(CiderValidationError, match="limit must be between 1 and 100."):
        validate_limit_offset(101, 0)


def test_validate_limit_offset_rejects_negative_offset():
    with pytest.raises(CiderValidationError, match="offset must be non-negative."):
        validate_limit_offset(10, -1)


def test_validate_limit_offset_accepts_valid():
    assert validate_limit_offset(10, 0) is None
    assert validate_limit_offset(100, 50) is None


# --- validate_playlist_id -------------------------------------------------


def test_validate_playlist_id_rejects_empty():
    with pytest.raises(CiderValidationError, match="playlist_id cannot be empty."):
        validate_playlist_id("   ")


def test_validate_playlist_id_accepts_non_empty():
    assert validate_playlist_id("p1") is None


# --- coerce_volume_param --------------------------------------------------


def test_coerce_volume_param_int_not_scaled():
    assert coerce_volume_param({"volume": 50}) == 50
    # An int 1 is NOT treated as a fraction: it stays 1.
    assert coerce_volume_param({"volume": 1}) == 1


def test_coerce_volume_param_float_fraction_scaled_to_percent():
    assert coerce_volume_param({"value": 0.5}) == 50
    # A float 1.0 IS treated as a fraction and scaled to 100.
    assert coerce_volume_param({"value": 1.0}) == 100
    assert coerce_volume_param({"level": 0.0}) == 0


def test_coerce_volume_param_string_numeric():
    assert coerce_volume_param({"volume": "50"}) == 50
    # A decimal string in [0, 1] is treated as a fraction.
    assert coerce_volume_param({"volume": "0.5"}) == 50


def test_coerce_volume_param_key_precedence():
    # First matching key in (volume, value, level, percent) wins.
    assert coerce_volume_param({"volume": 50, "value": 0.5}) == 50
    assert coerce_volume_param({"percent": 75}) == 75


def test_coerce_volume_param_missing_param_raises():
    with pytest.raises(CiderValidationError, match="set_volume requires a volume parameter."):
        coerce_volume_param({})


def test_coerce_volume_param_empty_string_raises():
    with pytest.raises(CiderValidationError, match="volume cannot be empty."):
        coerce_volume_param({"volume": "   "})


def test_coerce_volume_param_non_numeric_raises():
    with pytest.raises(CiderValidationError, match="volume must be numeric, got list."):
        coerce_volume_param({"volume": [1, 2]})
