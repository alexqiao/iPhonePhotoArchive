from __future__ import annotations

import pytest

from photoarchive.domain import AssetState, PhoneDevice, StateTransitionError, validate_transition


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (AssetState.DISCOVERED, AssetState.EXPORTED),
        (AssetState.EXPORTED, AssetState.UPLOADED),
        (AssetState.UPLOADED, AssetState.VERIFIED),
        (AssetState.VERIFIED, AssetState.SAFE_TO_DELETE),
        (AssetState.DISCOVERED, AssetState.FAILED),
        (AssetState.VERIFIED, AssetState.FAILED),
    ],
)
def test_legal_transitions(current: AssetState, target: AssetState) -> None:
    validate_transition(current, target)


def test_failed_can_only_restore_last_stable_state() -> None:
    validate_transition(
        AssetState.FAILED,
        AssetState.EXPORTED,
        last_stable_state=AssetState.EXPORTED,
    )
    with pytest.raises(StateTransitionError):
        validate_transition(
            AssetState.FAILED,
            AssetState.UPLOADED,
            last_stable_state=AssetState.EXPORTED,
        )


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (AssetState.DISCOVERED, AssetState.UPLOADED),
        (AssetState.UPLOADED, AssetState.SAFE_TO_DELETE),
        (AssetState.SAFE_TO_DELETE, AssetState.FAILED),
    ],
)
def test_illegal_transitions_fail(current: AssetState, target: AssetState) -> None:
    with pytest.raises(StateTransitionError):
        validate_transition(current, target)


def test_phone_used_capacity_is_derived_safely() -> None:
    device = PhoneDevice("device", "iPhone", "iPhone", False, True, True, True, 256_000, 64_000)
    assert device.used_capacity_bytes == 192_000

    unknown = PhoneDevice("device", "iPhone", "iPhone", False, True, True, True)
    assert unknown.used_capacity_bytes is None
