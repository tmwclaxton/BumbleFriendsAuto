"""One-shot UI dump for tuning selectors / swipe regions."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from src.config import ROOT, load_config
from src.device import connect, dump_artifacts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dump Bumble UI hierarchy + screenshot")
    parser.add_argument("--serial", help="ADB device serial (optional)")
    parser.add_argument("--config", type=Path, help="Path to config.yaml")
    parser.add_argument(
        "--dump-dir",
        type=Path,
        help="Output directory (default: config dump_dir)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = load_config(args.config)
    dump_dir = args.dump_dir or (ROOT / str(cfg["dump_dir"]))

    device = connect(args.serial)
    paths = dump_artifacts(device, dump_dir)
    print(f"XML: {paths['xml']}")
    print(f"PNG: {paths['png']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
