"""Build a complete Windows installer. Network access happens ONLY here.

Build host requirements: Windows x64, Python 3.13, uv, .NET Framework compiler.
  python scripts/build_windows_bundle.py --components-only
  python scripts/build_windows_bundle.py
The resulting installer contains Python, Node, Camoufox and all dependencies.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
LOCK = json.loads((ROOT / "packaging/components.json").read_text(encoding="utf-8"))
CACHE = ROOT / "data/build-cache"
VENDOR = ROOT / "vendor/aistudio"
DIST = ROOT / "dist"


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def archive(name):
    spec = LOCK["archives"][name]
    CACHE.mkdir(parents=True, exist_ok=True)
    target = CACHE / name
    if target.is_file() and digest(target) == spec["sha256"]:
        return target
    print(f"Build: downloading {name}", flush=True)
    partial = target.with_suffix(".part")
    request = urllib.request.Request(spec["url"], headers={"User-Agent": "job-bless-builder"})
    with urllib.request.urlopen(request, timeout=120) as response, partial.open("wb") as output:
        shutil.copyfileobj(response, output, length=1024 * 1024)
    if digest(partial) != spec["sha256"]:
        raise RuntimeError(f"Checksum mismatch for {name}; cached download was not accepted")
    partial.replace(target)
    return target


def unpack(source, target, *, strip_root=False):
    target = Path(target).resolve()
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source) as zip_file:
        for item in zip_file.infolist():
            parts = Path(item.filename).parts[1:] if strip_root else Path(item.filename).parts
            if not parts:
                continue
            destination = target.joinpath(*parts).resolve()
            if not destination.is_relative_to(target):
                raise RuntimeError("Archive contains an unsafe path")
            if item.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with zip_file.open(item) as input_file, destination.open("wb") as output:
                    shutil.copyfileobj(input_file, output)


def remove_build_directory(path, parent):
    path, parent = Path(path).resolve(), Path(parent).resolve()
    if path == parent or not path.is_relative_to(parent):
        raise RuntimeError(f"Refusing to remove a path outside {parent}")
    if path.exists():
        shutil.rmtree(path)


def run(args, *, cwd=None, env=None):
    subprocess.run([str(arg) for arg in args], cwd=cwd or ROOT, env=env, check=True)


def prepare_components():
    marker = VENDOR / "manifest.json"
    npm_lock = ROOT / "packaging/aistudio-package-lock.json"
    expected = {"format": 1, "platform": "win-x64", "commit": LOCK["aistudio_commit"],
                "version": LOCK["aistudio_version"], "npm_lock_sha256": digest(npm_lock),
                "archives": {name: value["sha256"] for name, value in LOCK["archives"].items() if not name.startswith("python")}}
    if marker.exists() and json.loads(marker.read_text()) == expected:
        print("Build: reusing prepared AIStudioToAPI components", flush=True)
        return
    stage = VENDOR.with_name("aistudio-stage")
    remove_build_directory(stage, ROOT / "vendor")
    unpack(archive("node-win64.zip"), stage / "node", strip_root=True)
    unpack(archive("aistudio-source.zip"), stage / "app", strip_root=True)
    unpack(archive("camoufox-win64.zip"), stage / "camoufox")
    shutil.copyfile(npm_lock, stage / "app/package-lock.json")
    node = stage / "node/node.exe"
    npm = stage / "node/node_modules/npm/bin/npm-cli.js"
    env = dict(os.environ, HUSKY="0", PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD="1")
    env["PATH"] = str(node.parent) + os.pathsep + env.get("PATH", "")
    run([node, npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=stage / "app", env=env)
    run([node, "node_modules/vite/bin/vite.js", "build"], cwd=stage / "app", env=env)
    run([node, npm, "prune", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=stage / "app", env=env)
    # The runtime never invokes npm. Retain Node's license and remove its
    # package manager; dependencies have already been prepared above.
    remove_build_directory(stage / "node/node_modules", stage)
    for name in ("npm", "npm.cmd", "npm.ps1", "npx", "npx.cmd", "npx.ps1", "install_tools.bat"):
        (stage / "node" / name).unlink(missing_ok=True)
    (stage / "manifest.json").write_text(json.dumps(expected, indent=2), encoding="utf-8")
    remove_build_directory(VENDOR, ROOT / "vendor")
    stage.rename(VENDOR)
    print("Build: bundled components ready", flush=True)


def build_distribution():
    if sys.version_info[:2] != (3, 13):
        raise RuntimeError("Build with Python 3.13 to match the embedded runtime")
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("The build machine needs uv for preparing Python dependencies")
    stage = DIST / "job-bless-windows"
    remove_build_directory(stage, DIST)
    (stage / "app").mkdir(parents=True)
    # Explicit allowlist: no dev config, database, auth, caches or git metadata.
    shutil.copytree(ROOT / "src", stage / "app/src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(ROOT / "main.py", stage / "app/main.py")
    shutil.copy2(ROOT / "scripts/windows/launch.py", stage / "app/launch.py")
    shutil.copytree(VENDOR, stage / "app/vendor/aistudio")
    shutil.copy2(ROOT / "scripts/windows/Start-job-bless.vbs", stage / "Start-job-bless.vbs")
    shutil.copy2(ROOT / "packaging/THIRD_PARTY_NOTICES.md", stage / "THIRD_PARTY_NOTICES.md")
    shutil.copy2(ROOT / "packaging/WINDOWS_README.txt", stage / "README.txt")
    python_dir = stage / "runtime/python"
    unpack(archive("python-win64.zip"), python_dir)
    site_packages = python_dir / "Lib/site-packages"
    run([uv, "pip", "install", "--python", sys.executable, "--target", site_packages, "--only-binary", ":all:",
         "-r", ROOT / "packaging/windows-requirements.txt"])
    (python_dir / "python313._pth").write_text("python313.zip\n.\nLib/site-packages\n../../app\nimport site\n", encoding="utf-8")
    # This verifies imports with the EMBEDDED interpreter, independent of the
    # builder's environment and any Python installation on the user's machine.
    run([python_dir / "python.exe", "-c", "from src.web.app import create_app; import playwright.sync_api; import asyncpg; print('Embedded runtime ready')"], cwd=stage)
    zip_path = DIST / "job-bless-windows.zip"
    print("Build: packing the offline distribution…", flush=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as output:
        for item in sorted(stage.rglob("*")):
            if item.is_file():
                output.write(item, item.relative_to(stage))
    build_id = digest(zip_path)[:16]
    csc = Path(os.environ.get("WINDIR", "C:/Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if not csc.is_file():
        raise RuntimeError("The build machine needs the .NET Framework C# compiler to produce Setup.exe")
    setup_source = (ROOT / "scripts/windows/Setup.cs").read_text(encoding="utf-8").replace("__BUILD_ID__", build_id)
    source_path = DIST / "Setup.generated.cs"
    source_path.write_text(setup_source, encoding="utf-8-sig")
    installer = DIST / "job-bless-Setup.exe"
    run([csc, "/nologo", "/target:winexe", "/platform:x64", "/optimize+", "/reference:System.Windows.Forms.dll",
         "/reference:System.Drawing.dll", "/reference:System.IO.Compression.dll", "/reference:System.IO.Compression.FileSystem.dll",
         f"/resource:{zip_path},payload.zip", f"/out:{installer}", source_path])
    print(f"Installer: {installer}\nPortable ZIP: {zip_path}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--components-only", action="store_true")
    args = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("This builder targets Windows x64")
    prepare_components()
    if not args.components_only:
        build_distribution()


if __name__ == "__main__":
    main()
