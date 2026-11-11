"""Serve validated local UI assets with the currently installed Phoenix backend."""

import argparse
import hashlib
import importlib
import json
from pathlib import Path, PureWindowsPath
import sys


def validate_artifact(server_dir: Path) -> dict[str, object]:
    static_dir = (server_dir / "static").resolve(strict=True)
    manifest_path = static_dir / ".vite" / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if not isinstance(manifest, dict) or "index.tsx" not in manifest:
        raise ValueError("Manifest must contain the index.tsx entry")
    entry = manifest["index.tsx"]
    if (
        not isinstance(entry, dict)
        or not isinstance(entry.get("file"), str)
        or not entry["file"].endswith(".js")
    ):
        raise ValueError("Manifest index.tsx entry must be JavaScript")
    assets: set[str] = set()
    for chunk in manifest.values():
        if not isinstance(chunk, dict) or not isinstance(chunk.get("file"), str):
            raise ValueError("Manifest chunk must name a local file")
        assets.add(chunk["file"])
        for field in ("css", "assets"):
            values = chunk.get(field, [])
            if not isinstance(values, list) or any(
                not isinstance(value, str) for value in values
            ):
                raise ValueError(f"Manifest {field} must be a list of files")
            assets.update(values)
        for field in ("imports", "dynamicImports"):
            values = chunk.get(field, [])
            if not isinstance(values, list) or any(
                not isinstance(value, str) or value not in manifest for value in values
            ):
                raise ValueError(f"Manifest {field} references a missing chunk")
    for name in assets:
        if (
            Path(name).is_absolute()
            or PureWindowsPath(name).is_absolute()
            or ":" in name
        ):
            raise ValueError("Manifest asset paths must be relative")
        asset = (static_dir / name).resolve(strict=True)
        if not asset.is_relative_to(static_dir) or not asset.is_file():
            raise ValueError("Manifest asset must be a file inside static")
    return {
        "status": "artifact-valid",
        "asset_count": len(assets),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-dir", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("phoenix_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        server_dir = args.server_dir.resolve(strict=True)
        report = validate_artifact(server_dir)
    except (OSError, ValueError) as error:
        print(f"Invalid frontend artifact: {error}", file=sys.stderr)
        return 2
    if args.check:
        print(json.dumps(report))
        return 0
    try:
        app_module = importlib.import_module("phoenix.server.app")
        cli_module = importlib.import_module("phoenix.server.main")
    except ModuleNotFoundError as error:
        print(f"Installed Phoenix backend dependency is unavailable: {error.name}", file=sys.stderr)
        return 3
    # The installed templates were initialized on import; only static assets change.
    app_module.SERVER_DIR = server_dir
    command = args.phoenix_args
    if command[:1] == ["--"]:
        command = command[1:]
    sys.argv = ["phoenix", *(command or ["serve"])]
    cli_module.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
