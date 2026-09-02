"""End-to-end OTA orchestration with #0011 flight-recorder audit events."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..flight_recorder import FlightRecorder
from .download import StreamingClient, download_verified_artifact
from .manifest import (
    CompatibilitySnapshot,
    ManifestError,
    PlatformTarget,
    PolicyDecision,
    RolloutContext,
    evaluate_manifest,
    verify_artifact,
    verify_signed_manifest,
)
from .state import (
    InvalidTransition,
    SafePointSnapshot,
    UpdateController,
    UpdateState,
)

_AUDIT_REASONS = frozenset(
    {
        "update_manifest_invalid",
        "update_policy_rejected",
        "update_download_failed",
        "update_verification_failed",
        "update_stage_failed",
        "update_safe_point_blocked",
        "update_self_test_failed",
        "update_crash_recovery",
        "candidate_failed",
        "update_state_corrupt",
        "update_state_incompatible",
    }
)


class FlightRecorderEventSink:
    """Translate OTA state writes into the strict #0011 event envelope.

    Arbitrary exception strings, release names and versions never enter the
    recorder. A deterministic opaque request id links one update's events.
    """

    def __init__(self, recorder: FlightRecorder):
        self.recorder = recorder

    @staticmethod
    def _request_id(release_id: str) -> str:
        return hashlib.sha256(release_id.encode("utf-8")).hexdigest()[:32]

    def emit(self, event_type: str, attributes: Mapping[str, Any]) -> None:
        state = event_type.removeprefix("ota.")
        sequence = attributes.get("sequence")
        release_id = attributes.get("release_id")
        if state not in {item.value for item in UpdateState}:
            return
        if type(sequence) is not int or not isinstance(release_id, str):
            return
        raw_reason = attributes.get("reason")
        reason_code = raw_reason if raw_reason in _AUDIT_REASONS else None
        self.recorder.try_record(
            f"update_{state}",
            component="ota",
            request_id=self._request_id(release_id),
            reason_code=reason_code,
            attributes={"update_state": state, "update_sequence": sequence},
        )

    def policy_rejected(self, reason_code: str = "update_policy_rejected") -> None:
        self.recorder.try_record(
            "update_policy_rejected",
            component="ota",
            reason_code=reason_code,
            attributes={"update_eligible": False},
        )

    def safe_point_blocked(self, release_id: str, sequence: int) -> None:
        self.recorder.try_record(
            "update_failed",
            component="ota",
            request_id=self._request_id(release_id),
            reason_code="update_safe_point_blocked",
            attributes={
                "update_state": UpdateState.WAITING_SAFE_POINT.value,
                "update_sequence": sequence,
            },
        )

    def fail_closed(self, reason_code: str) -> None:
        if reason_code not in _AUDIT_REASONS:
            reason_code = "candidate_failed"
        self.recorder.try_record(
            "update_failed",
            component="ota",
            reason_code=reason_code,
            attributes={"update_state": UpdateState.FAILED.value},
        )


class UpdateRuntime:
    """Prepare and activate one signed candidate without interrupting work."""

    def __init__(
        self,
        root: Path,
        *,
        recorder: FlightRecorder,
        download_client: StreamingClient,
    ):
        self.audit = FlightRecorderEventSink(recorder)
        self.controller = UpdateController(root, event_sink=self.audit)
        self.download_client = download_client

    def recover_on_launcher_start(self) -> bool:
        with self.controller.transaction():
            recovered = self.controller.recover_on_launcher_start()
        return recovered

    def prepare(
        self,
        raw_manifest: bytes | str | Mapping[str, Any],
        *,
        trusted_keys: Mapping[str, bytes | str],
        current_version: str,
        committed_sequence: int,
        compatibility: CompatibilitySnapshot,
        rollout: RolloutContext,
        target: PlatformTarget | None = None,
    ) -> PolicyDecision:
        """Verify policy, download, verify and stage a candidate for a safe point."""

        try:
            manifest = verify_signed_manifest(raw_manifest, trusted_keys)
        except ManifestError:
            self.audit.policy_rejected("update_manifest_invalid")
            raise
        self.controller.set_trusted_keys(trusted_keys)
        baseline = self.controller.committed_pointer()
        if (
            current_version != baseline.version
            or committed_sequence != baseline.sequence
        ):
            self.audit.fail_closed("update_state_incompatible")
            raise InvalidTransition(
                "caller OTA baseline does not match the durable committed pointer"
            )
        decision = evaluate_manifest(
            manifest,
            current_version=baseline.version,
            committed_sequence=baseline.sequence,
            compatibility=compatibility,
            rollout=rollout,
            target=target,
        )
        if not decision.eligible or decision.artifact is None:
            self.audit.policy_rejected()
            return decision

        artifact = decision.artifact
        phase_reason = "update_download_failed"
        with self.controller.transaction():
            self.controller.detect(manifest, artifact)
            try:
                downloaded = download_verified_artifact(
                    self.download_client,
                    artifact,
                    self.controller.releases / manifest.release_id,
                )
                self.controller.transition(UpdateState.DOWNLOADED)
                phase_reason = "update_verification_failed"
                verify_artifact(downloaded, artifact)
                self.controller.transition(UpdateState.VERIFIED)
                phase_reason = "update_stage_failed"
                self.controller.stage(manifest, artifact, downloaded)
                self.controller.wait_for_safe_point()
            except BaseException:
                try:
                    record = self.controller.state()
                    if record and record.get("state") in {
                        state.value
                        for state in (
                            UpdateState.DETECTED,
                            UpdateState.DOWNLOADED,
                            UpdateState.VERIFIED,
                            UpdateState.STAGED,
                            UpdateState.WAITING_SAFE_POINT,
                        )
                    }:
                        self.controller.transition(
                            UpdateState.FAILED, reason=phase_reason
                        )
                except Exception:  # noqa: BLE001 - preserve the original interrupt
                    self.audit.fail_closed(phase_reason)
                raise
        return decision

    def activate_and_self_test(
        self,
        safe_point: SafePointSnapshot | Callable[[], SafePointSnapshot],
        self_test: Callable[[Path], bool],
    ) -> UpdateState:
        """Activate only at a natural safe point; commit or restore the LKG."""

        with self.controller.transaction():
            record = self.controller.state()
            if not record:
                raise InvalidTransition("no prepared update is available")
            release = record["release"]
            snapshot = safe_point() if callable(safe_point) else safe_point
            if not isinstance(snapshot, SafePointSnapshot):
                raise TypeError("safe point provider must return SafePointSnapshot")
            if not snapshot.ready:
                self.audit.safe_point_blocked(
                    release["release_id"],
                    release["sequence"],
                )
                raise InvalidTransition(
                    "safe point is blocked by: " + ", ".join(snapshot.blockers()),
                )
            self.controller.activate(snapshot)
            self.controller.begin_self_test()
            candidate = self.controller.root / release["artifact"]
            try:
                passed = self_test(candidate) is True
            except (KeyboardInterrupt, SystemExit):
                self.controller.request_rollback("update_self_test_failed")
                self.controller.rollback("update_self_test_failed")
                raise
            except Exception:  # noqa: BLE001 - any candidate failure must roll back
                passed = False
            if passed:
                self.controller.commit()
                return UpdateState.COMMITTED
            self.controller.request_rollback("update_self_test_failed")
            self.controller.rollback("update_self_test_failed")
            return UpdateState.ROLLED_BACK
