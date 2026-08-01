from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

TOKEN_PATH = Path(__file__).resolve().parents[2] / "docs" / "design-tokens.json"
WEB_TOKEN_PATH = (
    Path(__file__).resolve().parents[2] / "web" / "src" / "theme" / "design-tokens.json"
)


def _tokens() -> Mapping[str, object]:
    parsed: object = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return cast(dict[str, object], parsed)


def _value(data: Mapping[str, object], *keys: str) -> object:
    current: object = data
    for key in keys:
        assert isinstance(current, dict)
        assert key in current
        current = current[key]
    return current


def _relative_luminance(hex_color: str) -> float:
    assert len(hex_color) == 7
    assert hex_color.startswith("#")
    channels = [int(hex_color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(first: str, second: str) -> float:
    lighter, darker = sorted(
        (_relative_luminance(first), _relative_luminance(second)),
        reverse=True,
    )
    return (lighter + 0.05) / (darker + 0.05)


def _color(data: Mapping[str, object], *keys: str) -> str:
    value = _value(data, "color", *keys)
    assert isinstance(value, str)
    return value


def test_design_tokens_are_valid_and_keep_accessibility_baselines() -> None:
    tokens = _tokens()

    assert _value(tokens, "meta", "accessibilityTarget") == "WCAG 2.2 AA"
    assert _value(tokens, "sizePx", "controlMinHeight") == 44
    assert _value(tokens, "sizePx", "targetMinWidth") == 44
    assert _value(tokens, "sizePx", "targetMinHeight") == 44
    assert _value(tokens, "motionMs", "reduced") == 0


def test_web_design_tokens_mirror_canonical_source() -> None:
    assert json.loads(WEB_TOKEN_PATH.read_text(encoding="utf-8")) == json.loads(
        TOKEN_PATH.read_text(encoding="utf-8")
    )


def test_design_token_text_pairs_meet_normal_text_contrast() -> None:
    tokens = _tokens()
    pairs = (
        ((_color(tokens, "text", "primary")), _color(tokens, "surface", "default")),
        ((_color(tokens, "text", "secondary")), _color(tokens, "surface", "default")),
        ((_color(tokens, "brand", "foreground")), _color(tokens, "brand", "default")),
        (
            _color(tokens, "status", "success", "foreground"),
            _color(tokens, "status", "success", "background"),
        ),
        (
            _color(tokens, "status", "partial", "foreground"),
            _color(tokens, "status", "partial", "background"),
        ),
        (
            _color(tokens, "status", "danger", "foreground"),
            _color(tokens, "status", "danger", "background"),
        ),
        (
            _color(tokens, "status", "info", "foreground"),
            _color(tokens, "status", "info", "background"),
        ),
        (
            _color(tokens, "status", "unknown", "foreground"),
            _color(tokens, "status", "unknown", "background"),
        ),
    )

    for foreground, background in pairs:
        assert _contrast(foreground, background) >= 4.5


def test_focus_and_control_boundaries_meet_non_text_contrast() -> None:
    tokens = _tokens()
    focus_ring = _color(tokens, "focus", "ring")
    focus_surfaces = (
        _color(tokens, "surface", "canvas"),
        _color(tokens, "surface", "default"),
        _color(tokens, "surface", "subtle"),
        _color(tokens, "surface", "inverse"),
        *(
            _color(tokens, "status", status, "background")
            for status in ("success", "partial", "danger", "info", "unknown")
        ),
    )

    for surface in focus_surfaces:
        assert _contrast(focus_ring, surface) >= 3

    default_surface = _color(tokens, "surface", "default")
    assert _contrast(_color(tokens, "border", "control"), default_surface) >= 3
    assert (
        _contrast(
            _color(tokens, "focus", "onBrandRing"),
            _color(tokens, "brand", "default"),
        )
        >= 3
    )
    assert (
        _contrast(
            _color(tokens, "focus", "onBrandOffset"),
            _color(tokens, "focus", "onBrandRing"),
        )
        >= 3
    )

    for status in ("success", "partial", "danger", "info", "unknown"):
        assert (
            _contrast(
                _color(tokens, "status", status, "border"),
                _color(tokens, "status", status, "background"),
            )
            >= 3
        )


def test_subtle_border_is_explicitly_decorative_only() -> None:
    tokens = _tokens()

    assert "Decorative divider only" in str(_value(tokens, "meta", "usage", "border.subtle"))
    assert _value(tokens, "forcedColors", "adjust") == "auto"
