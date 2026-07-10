# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Convert the Torch kudos checkpoint into runtime NumPy weights.

This is a maintainer tool, not a runtime dependency. It intentionally imports
torch so the application can load the generated .npz file without torch.
"""

from __future__ import annotations

import argparse
import pickle
from pathlib import Path

import numpy as np
import torch.nn as nn

EXPECTED_LAYER_TYPES = (
    nn.Linear,
    nn.ReLU,
    nn.Linear,
    nn.ReLU,
    nn.Dropout,
    nn.Linear,
    nn.ReLU,
    nn.Dropout,
    nn.Linear,
)
EXPECTED_LINEAR_SHAPES = (
    ((79, 47), (79,)),
    ((101, 79), (101,)),
    ((104, 101), (104,)),
    ((1, 104), (1,)),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "source",
        type=Path,
        nargs="?",
        default=Path("horde/classes/stable/kudos-v21-206.ckpt"),
        help="Torch pickle checkpoint to convert.",
    )
    parser.add_argument(
        "destination",
        type=Path,
        nargs="?",
        default=Path("horde/classes/stable/kudos-v21-206.npz"),
        help="Compressed NumPy archive to write.",
    )
    return parser.parse_args()


def validate_model(model: nn.Sequential) -> None:
    if not isinstance(model, nn.Sequential):
        raise TypeError(f"Expected torch.nn.Sequential, got {type(model)!r}")

    layer_types = tuple(type(layer) for layer in model)
    if layer_types != EXPECTED_LAYER_TYPES:
        raise ValueError(f"Unexpected kudos model architecture: {layer_types!r}")

    linear_layers = [layer for layer in model if isinstance(layer, nn.Linear)]
    shapes = tuple((tuple(layer.weight.shape), tuple(layer.bias.shape)) for layer in linear_layers)
    if shapes != EXPECTED_LINEAR_SHAPES:
        raise ValueError(f"Unexpected kudos linear layer shapes: {shapes!r}")

    if model.training:
        raise ValueError("Expected checkpoint to be saved in eval mode.")


def main() -> None:
    args = parse_args()
    with args.source.open("rb") as infile:
        model = pickle.load(infile)

    validate_model(model)

    arrays = {}
    for index, layer in enumerate(layer for layer in model if isinstance(layer, nn.Linear)):
        arrays[f"w{index}"] = layer.weight.detach().cpu().numpy().astype(np.float32)
        arrays[f"b{index}"] = layer.bias.detach().cpu().numpy().astype(np.float32)

    args.destination.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(args.destination, **arrays)


if __name__ == "__main__":
    main()
