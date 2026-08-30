#!/usr/bin/env python3
"""Cross-build x86_64 Linux artifacts for ``frpctl`` and ``frpdomain``.

The manager is pure Python, but Paramiko pulls in native extension modules
(``cryptography``, ``PyNaCl``, ``bcrypt``, ``cffi``, ``PyYAML``).  Those cannot be
recompiled on an unrelated architecture, so this script never compiles anything:
it downloads the official x86_64 manylinux wheels plus a relocatable x86_64
CPython from python-build-standalone and assembles them into a self-contained
tree.  The result therefore builds identically on aarch64, x86_64 or macOS
hosts; only a network connection is required.

Modes:

``bundle``      (default) a directory plus ``.tar.gz`` holding a private CPython,
                every dependency and ``bin/frpctl`` / ``bin/frpdomain`` launchers.
                The target host needs no Python and no pip.
``wheelhouse``  only the x86_64 wheels, for ``pip install --no-index
                --find-links`` on a machine that already has Python 3.11+.

Add ``--self-extracting`` in bundle mode for a single-file ``.run`` that unpacks
into a cache directory on first use and dispatches to either tool.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import tomllib
import urllib.parse
import urllib.request
from collections.abc import Iterable, Sequence
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE = "frpctl"
TOOLS = {"frpctl": "frpctl.cli", "frpdomain": "frpctl.domain_cli"}
PBS_API = (
    "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest"
)
PBS_RELEASES = "https://github.com/astral-sh/python-build-standalone/releases/download"
PBS_TRIPLE = "x86_64-unknown-linux-gnu"
# Ordered from oldest to newest glibc requirement so pip prefers the most
# portable wheel it can find for each dependency.
PLATFORMS = (
    "manylinux2014_x86_64",
    "manylinux_2_17_x86_64",
    "manylinux_2_28_x86_64",
    "manylinux_2_34_x86_64",
)


def log(message: str) -> None:
    print(f"[build] {message}", flush=True)


def run(command: Sequence[str]) -> None:
    log(" ".join(command))
    subprocess.run(command, check=True)


def project_metadata() -> tuple[str, list[str]]:
    with (REPO / "pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)
    project = data["project"]
    return project["version"], list(project.get("dependencies", []))


def download(url: str, target: Path) -> Path:
    if target.exists():
        log(f"cached {target.name}")
        return target
    log(f"fetching {url}")
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    with urllib.request.urlopen(url, timeout=120) as response:
        with partial.open("wb") as handle:
            shutil.copyfileobj(response, handle)
    partial.replace(target)
    return target


def resolve_cpython(
    python_version: str, release: str | None, stripped: bool
) -> tuple[str, str]:
    """Return the release tag and asset name of a relocatable x86_64 CPython."""
    flavour = "install_only_stripped" if stripped else "install_only"
    suffix = f"-{PBS_TRIPLE}-{flavour}.tar.gz"
    if release:
        api = f"https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/{release}"
    else:
        api = PBS_API
    request = urllib.request.Request(
        api, headers={"Accept": "application/vnd.github+json"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.load(response)
    tag = payload["tag_name"]
    candidates = [
        asset["name"]
        for asset in payload.get("assets", [])
        if asset["name"].startswith(f"cpython-{python_version}.")
        and asset["name"].endswith(suffix)
    ]
    if not candidates:
        raise SystemExit(
            f"no {PBS_TRIPLE} {flavour} asset for CPython {python_version} in "
            f"release {tag}"
        )
    return tag, max(candidates)


def cpython_url(tag: str, asset: str, base_url: str | None) -> str:
    """Build the download URL, optionally against a GitHub release mirror."""
    base = (base_url or PBS_RELEASES).rstrip("/")
    return f"{base}/{tag}/{urllib.parse.quote(asset)}"


def pip(args: Iterable[str]) -> None:
    run([sys.executable, "-m", "pip", *args])


def platform_args(python_version: str) -> list[str]:
    args = ["--only-binary", ":all:", "--implementation", "cp"]
    args += ["--python-version", python_version]
    for platform in PLATFORMS:
        args += ["--platform", platform]
    return args


def build_wheelhouse(
    dependencies: Sequence[str], python_version: str, wheelhouse: Path
) -> Path:
    if wheelhouse.exists():
        shutil.rmtree(wheelhouse)
    wheelhouse.mkdir(parents=True)
    pip(
        [
            "download",
            *platform_args(python_version),
            "--dest",
            str(wheelhouse),
            *dependencies,
        ]
    )
    return wheelhouse


def extract_cpython(archive: Path, destination: Path) -> Path:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    log(f"extracting {archive.name}")
    with tarfile.open(archive) as tar:
        # install_only archives contain a single top-level "python/" directory.
        members = [member for member in tar.getmembers() if member.name != "python"]
        for member in members:
            member.name = member.name.removeprefix("python/")
        _extract(tar, members, destination)
    interpreter = destination / "bin" / "python3"
    if not interpreter.exists():
        raise SystemExit(f"unexpected CPython layout in {archive}")
    return interpreter


def _extract(
    tar: tarfile.TarFile, members: list[tarfile.TarInfo], target: Path
) -> None:
    resolved = target.resolve()
    for member in members:
        candidate = (resolved / member.name).resolve()
        if resolved != candidate and resolved not in candidate.parents:
            raise SystemExit(f"refusing path traversal in archive: {member.name}")
    if sys.version_info >= (3, 12):
        tar.extractall(target, members=members, filter="tar")
    else:  # pragma: no cover - build host older than 3.12
        tar.extractall(target, members=members)


def site_packages(python_root: Path, python_version: str) -> Path:
    path = python_root / "lib" / f"python{python_version}" / "site-packages"
    if not path.exists():
        matches = sorted((python_root / "lib").glob("python3.*/site-packages"))
        if not matches:
            raise SystemExit(f"no site-packages under {python_root / 'lib'}")
        path = matches[0]
    return path


def install_dependencies(wheelhouse: Path, target: Path, python_version: str) -> None:
    pip(
        [
            "install",
            "--no-deps",
            "--no-index",
            "--find-links",
            str(wheelhouse),
            *platform_args(python_version),
            "--target",
            str(target),
            "--upgrade",
            *sorted(str(wheel) for wheel in wheelhouse.glob("*.whl")),
        ]
    )


def install_package(target: Path) -> None:
    source = REPO / "src" / PACKAGE
    destination = target / PACKAGE
    if destination.exists():
        shutil.rmtree(destination)
    log(f"copying {source} -> {destination}")
    shutil.copytree(
        source, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )


LAUNCHER = """#!/bin/sh
# Launcher generated by scripts/build_x86_64.py; keep it next to ../python.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec "$root/python/bin/python3" -sE -m {module} "$@"
"""


def write_launchers(bundle: Path) -> None:
    bin_dir = bundle / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name, module in TOOLS.items():
        launcher = bin_dir / name
        launcher.write_text(LAUNCHER.format(module=module), encoding="utf-8")
        launcher.chmod(0o755)


def prune(python_root: Path) -> None:
    """Drop build-time and test-only ballast from the bundled interpreter."""
    for pattern in (
        "lib/python3.*/test",
        "lib/python3.*/idlelib",
        "lib/python3.*/tkinter",
        "lib/python3.*/lib2to3",
        "lib/python3.*/config-*",
        "lib/python3.*/site-packages/pip*",
        "share/man",
    ):
        for path in python_root.glob(pattern):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
    for cache in python_root.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def verify_architecture(bundle: Path) -> list[str]:
    """Confirm every native artifact in the bundle is x86_64."""
    problems: list[str] = []
    seen: set[Path] = set()
    targets = [bundle / "python" / "bin" / "python3"]
    targets += sorted(bundle.rglob("*.so"))
    targets += sorted(bundle.rglob("*.so.*"))
    for path in targets:
        # bin/python3 and the libpython sonames are symlinks; follow them once.
        real = path.resolve()
        if real in seen or not real.is_file():
            continue
        seen.add(real)
        with real.open("rb") as handle:
            header = handle.read(20)
        if header[:4] != b"\x7fELF":
            continue
        machine = int.from_bytes(header[18:20], "little")
        if machine != 0x3E:  # EM_X86_64
            problems.append(f"{path.relative_to(bundle)}: e_machine=0x{machine:x}")
    checked = len(seen)
    log(f"verified {checked} ELF objects are x86_64")
    if not checked:
        problems.append("no ELF objects found in bundle")
    return problems


def make_tarball(bundle: Path, output: Path) -> Path:
    if output.exists():
        output.unlink()
    log(f"writing {output.name}")
    with tarfile.open(output, "w:gz") as tar:
        tar.add(bundle, arcname=bundle.name)
    return output


RUNNER = r"""#!/bin/sh
# Self-extracting frp-manager bundle (x86_64). Payload starts after the header.
set -eu
header_lines=__HEADER_LINES__
version=__VERSION__
digest=__DIGEST__
tool=$(basename -- "$0")
case "$tool" in
frpctl | frpdomain) ;;
*)
    if [ "$#" -eq 0 ]; then
        echo "usage: $0 {frpctl|frpdomain} [arguments...]" >&2
        exit 2
    fi
    tool=$1
    shift
    case "$tool" in
    frpctl | frpdomain) ;;
    *)
        echo "$0: unknown tool: $tool (expected frpctl or frpdomain)" >&2
        exit 2
        ;;
    esac
    ;;
esac
home=${FRP_MANAGER_HOME:-${XDG_CACHE_HOME:-$HOME/.cache}/frp-manager}
root=$home/$version-$digest
if [ ! -x "$root/bin/$tool" ]; then
    mkdir -p "$root.tmp.$$"
    tail -n +$((header_lines + 1)) -- "$0" | gzip -cd | tar -xf - -C "$root.tmp.$$" --strip-components=1
    rm -rf -- "$root"
    mv -f -- "$root.tmp.$$" "$root"
fi
exec "$root/bin/$tool" "$@"
__PAYLOAD_BELOW__
"""


def make_self_extracting(version: str, tarball: Path, output: Path) -> Path:
    digest = hashlib.sha256(tarball.read_bytes()).hexdigest()[:12]
    header = (
        RUNNER.replace("__VERSION__", version)
        .replace("__DIGEST__", digest)
        .replace("__HEADER_LINES__", str(RUNNER.count("\n")))
    )
    log(f"writing {output.name}")
    with output.open("wb") as handle:
        handle.write(header.encode())
        handle.write(tarball.read_bytes())
    output.chmod(0o755)
    return output


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("bundle", "wheelhouse"), default="bundle")
    parser.add_argument(
        "--python-version",
        default="3.13",
        help="CPython minor version to bundle and to resolve wheels for.",
    )
    parser.add_argument(
        "--pbs-release",
        default=None,
        help="python-build-standalone release tag (default: latest).",
    )
    parser.add_argument(
        "--release-base-url",
        default=None,
        metavar="URL",
        help="Mirror for GitHub release downloads, laid out as <URL>/<tag>/<asset>. "
        "Useful where objects.githubusercontent.com is unreachable, e.g. "
        "https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone",
    )
    parser.add_argument(
        "--cpython-archive",
        type=Path,
        default=None,
        metavar="PATH",
        help="Use this already-downloaded install_only tarball instead of fetching one.",
    )
    parser.add_argument(
        "--out", type=Path, default=REPO / "dist", help="Output directory."
    )
    parser.add_argument(
        "--work",
        type=Path,
        default=REPO / "build" / "x86_64",
        help="Scratch directory.",
    )
    parser.add_argument(
        "--no-strip",
        action="store_true",
        help="Bundle the unstripped CPython (larger, keeps debug symbols).",
    )
    parser.add_argument(
        "--self-extracting",
        action="store_true",
        help="Also emit a single-file .run dispatcher.",
    )
    args = parser.parse_args(argv)

    version, dependencies = project_metadata()
    args.out.mkdir(parents=True, exist_ok=True)
    args.work.mkdir(parents=True, exist_ok=True)

    if args.mode == "wheelhouse":
        wheelhouse = build_wheelhouse(
            dependencies, args.python_version, args.out / "wheelhouse-x86_64"
        )
        count = len(list(wheelhouse.glob("*.whl")))
        log(f"wheelhouse ready: {wheelhouse} ({count} wheels)")
        log(
            "install with: pip install --no-index --find-links "
            f"{wheelhouse.name} frp-multinode-manager"
        )
        return 0

    wheelhouse = build_wheelhouse(
        dependencies, args.python_version, args.work / "wheelhouse"
    )
    if args.cpython_archive:
        archive = args.cpython_archive
        if not archive.is_file():
            raise SystemExit(f"no such CPython archive: {archive}")
    else:
        tag, asset = resolve_cpython(
            args.python_version, args.pbs_release, not args.no_strip
        )
        archive = download(
            cpython_url(tag, asset, args.release_base_url),
            args.work / "cache" / asset,
        )

    bundle = args.out / f"frp-manager-{version}-linux-x86_64"
    if bundle.exists():
        shutil.rmtree(bundle)
    python_root = bundle / "python"
    extract_cpython(archive, python_root)
    packages = site_packages(python_root, args.python_version)
    install_dependencies(wheelhouse, packages, args.python_version)
    install_package(packages)
    write_launchers(bundle)
    prune(python_root)
    (bundle / "VERSION").write_text(
        f"frp-multinode-manager {version}\ncpython {args.python_version} ({PBS_TRIPLE})\n",
        encoding="utf-8",
    )

    problems = verify_architecture(bundle)
    if problems:
        for problem in problems:
            print(f"[build] ERROR {problem}", file=sys.stderr)
        return 1

    tarball = make_tarball(bundle, args.out / f"{bundle.name}.tar.gz")
    if args.self_extracting:
        make_self_extracting(
            version, tarball, args.out / f"frp-manager-{version}-linux-x86_64.run"
        )
    size = tarball.stat().st_size / 1_048_576
    log(f"done: {tarball} ({size:.1f} MiB)")
    log(f"run on the target host: {bundle.name}/bin/frpctl --help")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
