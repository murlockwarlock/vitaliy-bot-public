from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


VALID_MODES = {"normal", "clinical", "crisis"}
BOOLEAN_FIELDS = (
    "has_experience",
    "has_question",
    "is_skeptic",
    "is_empty",
    "show_offer",
)


@dataclass(frozen=True)
class FunnelAnalysis:
    mode: str
    has_experience: bool
    has_question: bool
    is_skeptic: bool
    is_empty: bool
    show_offer: bool
    reply: str
    bridge: str

    @property
    def offer_eligible(self) -> bool:
        return (
            self.mode == "normal"
            and self.show_offer
            and bool(self.bridge.strip())
            and not self.is_empty
            and not self.is_skeptic
        )


class FunnelParseError(ValueError):
    pass


def _extract_object(raw_text: str) -> dict[str, Any]:
    text = (raw_text or "").strip()
    if not text:
        raise FunnelParseError("empty response")

    candidates = [text]
    first = text.find("{")
    last = text.rfind("}")
    if first >= 0 and last > first:
        candidates.append(text[first : last + 1])

    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            return value
    raise FunnelParseError("invalid JSON object")


def parse_funnel_analysis(raw_text: str, *, max_chars: int = 40_000) -> FunnelAnalysis:
    if not isinstance(raw_text, str) or len(raw_text) > max_chars:
        raise FunnelParseError("response is empty or too large")

    payload = _extract_object(raw_text)
    mode = payload.get("mode")
    if not isinstance(mode, str) or mode.strip().lower() not in VALID_MODES:
        raise FunnelParseError("invalid mode")
    mode = mode.strip().lower()

    flags: dict[str, bool] = {}
    for field in BOOLEAN_FIELDS:
        value = payload.get(field, False)
        if not isinstance(value, bool):
            raise FunnelParseError(f"invalid {field}")
        flags[field] = value

    reply = payload.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        raise FunnelParseError("missing reply")
    bridge = payload.get("bridge", "")
    if bridge is None:
        bridge = ""
    if not isinstance(bridge, str):
        raise FunnelParseError("invalid bridge")

    return FunnelAnalysis(
        mode=mode,
        reply=reply.strip(),
        bridge=bridge.strip(),
        **flags,
    )


def render_funnel_fallback_text(text: str | None) -> str:
    fallback = (text or "").strip()
    return fallback or "Не удалось разобрать ответ. Попробуй ещё раз или выбери другой раздел."
