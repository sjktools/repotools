"""Check that every distribution in a directory (default dist/) has a non-empty
PEP 740 publish attestation beside it, as astral-sh/attest-action writes them:
`<file>.publish.attestation`.

uv publish uploads a file's attestation if it finds one and quietly uploads the
file without one if not, so the publish jobs run this between attest-action and
uv publish, to stop rather than publish a release with no provenance. The
Attestations workflow runs it on PRs that could break that, without uploading.

Prints a GitHub Actions ::error:: line for each problem, which also reads fine
outside Actions."""

import argparse
import sys
from pathlib import Path

SUFFIX = ".publish.attestation"


def distributions(dist: Path) -> list[Path]:
    """The wheels and sdists in dist, sorted."""
    return sorted([*dist.glob("*.whl"), *dist.glob("*.tar.gz")])


def problems(dist: Path) -> tuple[list[str], int]:
    """Each distribution in dist with no attestation or an empty one, and how
    many distributions there are. No distribution at all is a problem too."""
    files = distributions(dist)
    if not files:
        return [f"No distributions found in {dist.as_posix()}/"], 0
    found: list[str] = []
    for file in files:
        attestation = file.with_name(file.name + SUFFIX)
        if not attestation.is_file():
            found.append(f"No attestation for {file.as_posix()}")
        elif not attestation.stat().st_size:
            found.append(f"Empty attestation for {file.as_posix()}")
    return found, len(files)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) > 1:
        print("usage: check_attestations.py [DIST_DIR]", file=sys.stderr)
        return 2
    dist = Path(args[0] if args else "dist")
    found, count = problems(dist)
    for problem in found:
        print(f"::error::{problem}")
    if found:
        return 1
    print(f"all {count} distributions in {dist.as_posix()}/ have an attestation")
    return 0


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="repotools check-attestations", description=__doc__.splitlines()[0])
    parser.add_argument("dist", nargs="?", default="dist", help="directory of distributions (default: dist)")
    return main([parser.parse_args(argv).dist])
