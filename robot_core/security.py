from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
  return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_payload(payload: dict[str, Any], secret: str) -> str:
  mac = hmac.new(secret.encode("utf-8"), _canonical_bytes(payload), hashlib.sha256)
  return mac.hexdigest()


def verify_payload_signature(payload: dict[str, Any], signature: str, secret: str) -> bool:
  expected = sign_payload(payload, secret)
  return hmac.compare_digest(expected, signature)


@dataclass(frozen=True)
class TopicAccessPolicy:
  allowed_publish_topics: tuple[str, ...]
  allowed_subscribe_topics: tuple[str, ...]


def check_publish_allowed(topic: str, policy: TopicAccessPolicy) -> bool:
  return topic in policy.allowed_publish_topics


def check_subscribe_allowed(topic: str, policy: TopicAccessPolicy) -> bool:
  return topic in policy.allowed_subscribe_topics
