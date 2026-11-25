"""Signature verification as a *third-party* receiver would implement it: no imports from the platform's code.

Scheme: Webhook-Signature: v1=<hex HMAC-SHA256(secret, "{Webhook-Id}.{Webhook-Timestamp}.{raw body}")>
Several space-separated signatures may be sent during a secret rotation; any match is accepted.
"""
import hashlib
import hmac
import time
from dataclasses import dataclass


@dataclass
class Verification:
    valid: bool
    reason: str | None = None


def verify(secret: str, event_id: str | None, timestamp: str | None, signature: str | None, body: bytes,
           tolerance_seconds: int, now: float | None = None) -> Verification:
    if not (event_id and timestamp and signature):
        return Verification(False, "missing Webhook-Id, Webhook-Timestamp or Webhook-Signature header")
    try:
        age = abs((now or time.time()) - int(timestamp))
    except ValueError:
        return Verification(False, "Webhook-Timestamp is not a unix timestamp")
    if age > tolerance_seconds:
        return Verification(False, f"timestamp is {int(age)} s old (tolerance {tolerance_seconds} s): possible replay")
    mac = hmac.new(secret.encode(), f"{event_id}.{timestamp}.".encode() + body, hashlib.sha256)
    expected = "v1=" + mac.hexdigest()
    if any(hmac.compare_digest(expected, candidate.strip()) for candidate in signature.split(" ")):
        return Verification(True)
    return Verification(False, "signature does not match the body (wrong secret or tampered payload)")
