"""Enroll a headless host using approval from another device."""

from __future__ import annotations

import datetime as dt
import sys

from .contracts import BootstrapError, EnrollmentAttempt, EnrollmentContext, EnrollmentSession, WorkerResponseError
from .enrollment import (
    _cached_host_credential,
    _complete_host_enrollment,
    _device_poll_url,
    _enrollment_leader_lock,
    _hosted_enrollment_routes,
    _load_pending_enrollment_session,
    _store_pending_enrollment,
    _validate_pending_callback_approval_url,
)
from .metadata import _hosted_api_base_url
from .storage import _state_path
from .validation import _datetime_value, _int_value, _non_empty, _string_value
from .worker import _post_json_response

DEVICE_SESSION_TTL_SECONDS = 15 * 60
DEVICE_SESSION_CLOCK_SKEW_SECONDS = 5 * 60


def _obtain_device_host_credential(context: EnrollmentContext) -> EnrollmentAttempt:
    """Persist the approval before displaying it, and resume it on later runs."""

    credential = _cached_host_credential(context)
    if credential is not None:
        return EnrollmentAttempt(credential=credential)
    state_path = _state_path()
    with _enrollment_leader_lock(context, state_path) as leader:
        if not leader:
            return EnrollmentAttempt(credential=_cached_host_credential(context), reason="enrollment_in_progress")
        credential = _cached_host_credential(context)
        if credential is not None:
            return EnrollmentAttempt(credential=credential)
        session = _load_pending_enrollment_session(context, state_path)
        if session is None:
            created = _create_device_enrollment_session(context)
            session, _ = _store_pending_enrollment(context, state_path, created)
        if session.approval_url is not None:
            print(
                f"Open this link on a device with a browser to approve this host:\n{session.approval_url}\n"
                f"Approval expires at {session.expires_at.isoformat()}. "
                "Run this command again to resume if approval is still pending.",
                file=sys.stderr,
                flush=True,
            )
        else:
            print("A browser enrollment is already pending for this host; waiting for its approval.", file=sys.stderr)
        return _complete_host_enrollment(context, state_path, session)


def _create_device_enrollment_session(context: EnrollmentContext) -> EnrollmentSession:
    """Request a bounded approval session without a browser or loopback server."""

    api_base_url = _hosted_api_base_url()
    try:
        response = _post_json_response(
            f"{api_base_url}/v1/instruction-hub/host-enrollments/device-sessions",
            None,
            {
                "deployment_instance_id": context.deployment_instance_id,
                "target": context.metadata.target,
                "plugin_id": context.metadata.plugin_id,
                "plugin_version": context.metadata.plugin_version,
                "package_id": context.metadata.package_id,
                "bootstrap_version": context.metadata.bootstrap_version,
                "toolchain_version": context.metadata.toolchain_version,
                "host_instance_id": context.host_instance_id,
                "host_label": context.host_label,
                "host_platform": context.host_platform,
            },
            label="device enrollment response",
            allow_redirects=False,
        )
    except WorkerResponseError as exc:
        if exc.status_code in {404, 405}:
            raise BootstrapError(
                "The hosted API does not support device enrollment or does not recognize this worker deployment. "
                "Check hosted_api_base_url and deploy the device-enrollment API before retrying."
            ) from exc
        if exc.status_code == 429:
            raise BootstrapError("Too many pending device enrollment requests; wait before retrying.") from exc
        raise
    session_id = _non_empty(_string_value(response.get("session_id")))
    device_code = _non_empty(_string_value(response.get("device_code")))
    if session_id is None or device_code is None:
        raise BootstrapError("device enrollment response missing session_id or device_code")
    poll_url = _device_poll_url(api_base_url, session_id)
    approval_url = _validate_pending_callback_approval_url(
        {"approval_url": _string_value(response.get("approval_url")) or ""},
        _hosted_enrollment_routes(context.dashboard_base_url),
    )
    expires_at = _datetime_value(response.get("expires_at"), "device enrollment expires_at")
    lifetime = (expires_at - dt.datetime.now(dt.timezone.utc)).total_seconds()
    # Accept a slightly slow host clock without changing the server's expiry.
    if not 0 < lifetime <= DEVICE_SESSION_TTL_SECONDS + DEVICE_SESSION_CLOCK_SKEW_SECONDS:
        raise BootstrapError("device enrollment response expiry exceeds its 15-minute lifetime and clock allowance")
    interval = _int_value(response.get("poll_interval_seconds"), "device enrollment poll interval")
    if not 1 <= interval <= 30:
        raise BootstrapError("device enrollment poll interval must be between 1 and 30 seconds")
    return EnrollmentSession(
        session_id=session_id,
        deployment_instance_id=context.deployment_instance_id,
        device_code=device_code,
        poll_url=poll_url,
        expires_at=expires_at,
        poll_interval_seconds=interval,
        hosted_api_base_url=api_base_url,
        approval_url=approval_url,
    )
