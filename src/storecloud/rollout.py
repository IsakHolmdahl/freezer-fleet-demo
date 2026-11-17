"""Rollout engine - pure logic for software rollouts. No HTTP, no background threads."""

from collections.abc import Callable
from dataclasses import dataclass

from storecloud.db import Database, Rollout, RolloutFreezer
from storecloud.storeapi import StoreApi, StoreApiError


@dataclass
class RolloutEngine:
    """Core rollout logic. Takes clock and StoreApi as parameters for testability."""

    api: StoreApi
    db: Database
    clock: Callable[[], float]

    # Public API --------------------------------------------------------------

    def start_rollout(
        self,
        version: str,
        freezer_ids: list[str] | None = None,
        canary_count: int = 2,
        max_attempts: int = 3,
        max_failed: int = 1,
    ) -> Rollout:
        """Create a new rollout for the given version."""
        # Validate version exists
        software = self.api.software()
        available_versions = {s.version for s in software}
        if version not in available_versions:
            raise StoreApiError(404, "unknown_version", f"Unknown version: {version}")

        # Determine freezers to include
        all_freezers = self.api.freezers()
        if freezer_ids is None:
            # All freezers not already on this version
            freezer_ids = [f.id for f in all_freezers if f.software_version != version]
        else:
            # Validate explicit freezer IDs exist
            valid_ids = {f.id for f in all_freezers}
            for fid in freezer_ids:
                if fid not in valid_ids:
                    raise StoreApiError(422, "unknown_freezer", f"Unknown freezer: {fid}")
            # Exclude freezers already on this version even if explicitly specified
            # Use store API freezers (all_freezers) for version check since DB may not be synced yet
            api_versions = {f.id: f.software_version for f in all_freezers}
            freezer_ids = [fid for fid in freezer_ids if api_versions.get(fid) != version]

        # Create rollout
        now = self.clock()
        rollout = Rollout(
            version=version,
            state="running",
            canary_count=canary_count,
            max_attempts=max_attempts,
            max_failed=max_failed,
            created_at=now,
        )

        # Create rollout first
        rollout = self.db.create_rollout(rollout)

        # Add freezer entries
        for i, fid in enumerate(freezer_ids):
            rf = RolloutFreezer(
                rollout_id=rollout.id,
                freezer_id=fid,
                canary=(i < canary_count),
                state="pending",
                attempts=0,
            )
            self.db.save_rollout_freezer(rf)

        # Re-fetch with freezers loaded
        return self.db.get_rollout(rollout.id)

    def process_rollout(self, rollout_id: int) -> Rollout:
        """Process a rollout: send install commands, advance states."""
        rollout = self.db.get_rollout(rollout_id)
        if not rollout or rollout.state != "running":
            return rollout

        # Separate canaries and non-canaries
        canaries = [rf for rf in rollout.freezers if rf.canary]
        non_canaries = [rf for rf in rollout.freezers if not rf.canary]

        # Canary phase: process canaries until all installed
        canaries_not_installed = [rf for rf in canaries if rf.state != "installed"]
        if canaries_not_installed:
            self._process_freezers(canaries_not_installed, rollout)
            return self._to_dict(self.db.get_rollout(rollout_id))

        # Main phase: process non-canaries
        non_canaries_not_installed = [rf for rf in non_canaries if rf.state != "installed"]
        self._process_freezers(non_canaries_not_installed, rollout)

        # Check rollout completion
        updated = self.db.get_rollout(rollout_id)
        return self._check_rollout_completion(updated)

    def pause_rollout(self, rollout_id: int) -> Rollout:
        """Pause a running rollout."""
        rollout = self.db.get_rollout(rollout_id)
        if rollout and rollout.state == "running":
            rollout.state = "paused"
            self.db.update_rollout(rollout)
        return rollout

    def resume_rollout(self, rollout_id: int) -> Rollout:
        """Resume a paused rollout: reset failed freezers to pending with max_attempts new attempts."""
        rollout = self.db.get_rollout(rollout_id)
        if rollout and rollout.state == "paused":
            rollout.state = "running"
            # Reset failed freezers to pending; they get max_attempts new attempts (attempts reset to 0)
            for rf in rollout.freezers:
                if rf.state == "failed":
                    rf.state = "pending"
                    rf.attempts = 0
                    rf.error = None
                    self.db.update_freezer_state(
                        rf.freezer_id, rollout.id, "pending", 0, None, None
                    )
            self.db.update_rollout(rollout)
        return rollout

    def cancel_rollout(self, rollout_id: int) -> Rollout:
        """Cancel a running or paused rollout."""
        rollout = self.db.get_rollout(rollout_id)
        if rollout and rollout.state in ("running", "paused"):
            rollout.state = "cancelled"
            rollout.finished_at = self.clock()
            self.db.update_rollout(rollout)
        return rollout

    def sync_freezer_states(self, rollout_id: int) -> Rollout:
        """Sync freezer install states from store API."""
        rollout = self.db.get_rollout(rollout_id)
        if not rollout:
            return rollout

        for rf in rollout.freezers:
            if rf.state in ("sent", "downloading", "installing"):
                try:
                    freezer_state = self.api.freezer(rf.freezer_id)
                    install_state = freezer_state.install
                    if install_state:
                        if install_state.state == "downloading":
                            rf.state = "downloading"
                        elif install_state.state == "installing":
                            rf.state = "installing"
                        elif install_state.state == "installed":
                            rf.state = "installed"
                            rf.installed_at = self.clock()
                            # Preserve any error from previous failed attempts
                            self.db.update_freezer_state(
                                rf.freezer_id, rollout.id, "installed", rf.attempts, rf.error, rf.installed_at
                            )
                            # Update freezer software version
                            freezer = self.db.freezer(rf.freezer_id)
                            if freezer:
                                freezer.software_version = rollout.version
                                # Save directly to DB
                                with self.db.session() as session, session.begin():
                                    session.merge(freezer)
                        elif install_state.state == "failed":
                            rf.state = "failed"
                            rf.error = install_state.error or "Install failed"
                except StoreApiError:
                    # Keep current state, will retry on next poll
                    pass

        self.db.update_rollout(rollout)
        return self._check_rollout_completion(self.db.get_rollout(rollout_id))

    def get_rollout(self, rollout_id: int) -> Rollout | None:
        return self.db.get_rollout(rollout_id)

    def list_rollouts(self, limit: int) -> list[Rollout]:
        return self.db.list_rollouts(limit)

    # Private helpers ---------------------------------------------------------

    def _process_freezers(self, freezers: list[RolloutFreezer], rollout: Rollout) -> None:
        for rf in freezers:
            if rf.state != "pending":
                continue

            # Check if max attempts reached
            if rf.attempts >= rollout.max_attempts:
                rf.state = "failed"
                self.db.update_freezer_state(
                    rf.freezer_id, rollout.id, "failed", rf.attempts,
                    rf.error or "Max attempts reached", None
                )
                continue

            try:
                install = self.api.install(rf.freezer_id, rollout.version)
                if install.state == "downloading":
                    rf.state = "sent"
                    rf.attempts += 1
                    # Preserve any error from previous failed attempt
                    self.db.update_freezer_state(
                        rf.freezer_id, rollout.id, "sent", rf.attempts, rf.error, None
                    )
                elif install.state == "installing":
                    rf.state = "downloading"
                    rf.attempts += 1
                    # Preserve any error from previous failed attempt
                    self.db.update_freezer_state(
                        rf.freezer_id, rollout.id, "downloading", rf.attempts, rf.error, None
                    )
                elif install.state == "installed":
                    rf.state = "installed"
                    rf.installed_at = self.clock()
                    # Keep the error from previous failed attempt if any
                    self.db.update_freezer_state(
                        rf.freezer_id, rollout.id, "installed", rf.attempts, rf.error, rf.installed_at
                    )
                    # Update freezer software version
                    freezer = self.db.freezer(rf.freezer_id)
                    if freezer:
                        freezer.software_version = rollout.version
                        # Save directly to DB
                        with self.db.session() as session, session.begin():
                            session.merge(freezer)
                elif install.state == "failed":
                    rf.attempts += 1
                    rf.state = "failed"
                    rf.error = install.error or "Install failed"
                    self.db.update_freezer_state(
                        rf.freezer_id, rollout.id, "failed", rf.attempts, rf.error, None
                    )
            except StoreApiError as e:
                if e.code in ("offline", "busy"):
                    # Not a failure - keep pending, don't increment attempts
                    pass
                elif e.code == "already_installed":
                    rf.state = "installed"
                    rf.installed_at = self.clock()
                    self.db.update_freezer_state(
                        rf.freezer_id, rollout.id, "installed", rf.attempts, None, rf.installed_at
                    )
                    freezer = self.db.freezer(rf.freezer_id)
                    if freezer:
                        freezer.software_version = rollout.version
                        # Save directly to DB
                        with self.db.session() as session, session.begin():
                            session.merge(freezer)
                elif e.code == "install_in_progress":
                    rf.state = "downloading"
                    self.db.update_freezer_state(
                        rf.freezer_id, rollout.id, "downloading", rf.attempts, None, None
                    )
                else:
                    # Other errors count as attempt
                    rf.attempts += 1
                    rf.error = str(e)
                    if rf.attempts >= rollout.max_attempts:
                        rf.state = "failed"
                    self.db.update_freezer_state(
                        rf.freezer_id, rollout.id, rf.state or "failed", rf.attempts, rf.error, None
                    )

    def _check_rollout_completion(self, rollout: Rollout) -> Rollout:
        if not rollout:
            return rollout

        failed_count = sum(1 for rf in rollout.freezers if rf.state == "failed")
        installed_count = sum(1 for rf in rollout.freezers if rf.state == "installed")
        total = len(rollout.freezers)

        if installed_count == total:
            rollout.state = "completed"
            rollout.finished_at = self.clock()
            self.db.update_rollout(rollout)
            return rollout

        # Check if there are failed freezers that can be retried (attempts < max_attempts)
        # Reset them to pending so they get retried on next process_rollout call
        retryable_failed = [
            rf for rf in rollout.freezers
            if rf.state == "failed" and rf.attempts < rollout.max_attempts
        ]
        if retryable_failed:
            # Reset retryable failed freezers to pending for retry
            # Preserve the error from the failed attempt
            for rf in retryable_failed:
                rf.state = "pending"
                self.db.update_freezer_state(
                    rf.freezer_id, rollout.id, "pending", rf.attempts, rf.error, None
                )
            # Keep rollout running to process retries
            return rollout

        # Check if there are no pending or in-progress freezers
        pending_count = sum(1 for rf in rollout.freezers if rf.state == "pending")
        in_progress_count = sum(
            1 for rf in rollout.freezers
            if rf.state in ("sent", "downloading", "installing")
        )

        # If all freezers are in a terminal state (installed or failed with no attempts left)
        if in_progress_count == 0 and pending_count == 0:
            # If there are any failed freezers, pause for operator intervention
            # (even if within max_failed limit - operator must acknowledge)
            if failed_count > 0:
                rollout.state = "paused"
            else:
                rollout.state = "completed"
            rollout.finished_at = self.clock()
            self.db.update_rollout(rollout)
        # During rollout: if failed_count > max_failed, pause early
        elif failed_count > rollout.max_failed:
            rollout.state = "paused"
            self.db.update_rollout(rollout)

        return rollout

    def _to_dict(self, rollout: Rollout | None) -> Rollout | None:
        return rollout