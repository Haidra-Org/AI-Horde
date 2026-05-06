# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from horde.consts import (
    KNOWN_POST_PROCESSORS,
    KNOWN_SAMPLERS,
    KNOWN_UPSCALERS,
    SECOND_ORDER_SAMPLERS,
)


class TestKnownPostProcessors:
    def test_values_are_numeric(self):
        for name, value in KNOWN_POST_PROCESSORS.items():
            assert isinstance(value, (int, float)), f"{name} has non-numeric value: {value}"

    def test_not_empty(self):
        assert len(KNOWN_POST_PROCESSORS) > 0


class TestKnownSamplers:
    def test_not_empty(self):
        assert len(KNOWN_SAMPLERS) > 0

    def test_second_order_is_subset(self):
        for sampler in SECOND_ORDER_SAMPLERS:
            assert sampler in KNOWN_SAMPLERS, f"Second-order sampler '{sampler}' not in KNOWN_SAMPLERS"


class TestKnownUpscalers:
    def test_upscalers_are_post_processors(self):
        for upscaler in KNOWN_UPSCALERS:
            assert upscaler in KNOWN_POST_PROCESSORS, f"Upscaler '{upscaler}' not in KNOWN_POST_PROCESSORS"
