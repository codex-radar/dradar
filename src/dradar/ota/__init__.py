"""Fail-closed building blocks for the DRadar launcher OTA protocol.

This package intentionally has no runner or telemetry side effects.  The
runtime integration is deferred until the versioned event envelope is stable.
"""

from .download import VerifiedArtifact
from .manifest import (
    Artifact,
    CompatibilitySnapshot,
    ManifestError,
    PlatformTarget,
    PolicyDecision,
    ReleaseManifest,
    RolloutContext,
    evaluate_manifest,
    verify_artifact,
    verify_signed_manifest,
)
from .runtime import FlightRecorderEventSink, UpdateRuntime
from .state import (
    InvalidTransition,
    SafePointSnapshot,
    UpdateController,
    UpdateLock,
    UpdateLockBusy,
    UpdateState,
)

__all__ = [
    "Artifact",
    "CompatibilitySnapshot",
    "FlightRecorderEventSink",
    "InvalidTransition",
    "ManifestError",
    "PlatformTarget",
    "PolicyDecision",
    "ReleaseManifest",
    "RolloutContext",
    "SafePointSnapshot",
    "UpdateController",
    "UpdateLock",
    "UpdateLockBusy",
    "UpdateRuntime",
    "UpdateState",
    "VerifiedArtifact",
    "evaluate_manifest",
    "verify_artifact",
    "verify_signed_manifest",
]
