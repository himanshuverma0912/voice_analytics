"""Command-line entry points.

These are what a container image exposes. The library itself never imports
this package -- keeping the dependency one-way means the importable API stays
usable without argparse or any CLI concern.
"""

from voice_analytics.cli.main import build_parser, main

__all__ = ["build_parser", "main"]
