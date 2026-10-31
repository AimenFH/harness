"""Executable and Stage 1 import compatibility; CLI implementation is in cli.py."""

import sys

import cli
from application import build_model, build_sandbox
from cli import HARNESS_DIR, parse_config  # Existing callers import these here.


def main(argv=None):
    # Preserve model/sandbox injection used by the Stage 1 tests and demo.
    return cli.main(argv, model_factory=build_model, sandbox_factory=build_sandbox)


if __name__ == '__main__':
    sys.exit(main())
