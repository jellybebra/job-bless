"""Fetch a pinned Linux browser into an image and verify before extracting."""

import hashlib
from pathlib import Path
import sys
import tempfile
import urllib.request
import zipfile

ARCHIVES = {
    "amd64": ("x86_64", "61e1ec455e021720af38a5cc5ff7566121363cb5b82b72f24e381ba2676a4888"),
    "arm64": ("arm64", "11c4ef8277e7eca4ebbf1970048b76734ff87d5cb7e180336dd0cbe33bf93e41"),
}

HH_ARCHIVES = {
    "amd64": ("x86_64", "5720d45b894ce1770543de024c6f10d514b38be560fa2dc3226b3d8586caf672"),
    "arm64": ("arm64", "60447260af8bebdb0ec3f2aa72f687b879e5598367303de2e3fdbc7a5be8c124"),
}


def main():
    architecture, destination = sys.argv[1:3]
    hh = sys.argv[3:] == ["hh"]
    version = "152.0.4-beta.30" if hh else "135.0.1-beta.24"
    arch, expected = (HH_ARCHIVES if hh else ARCHIVES)[architecture]
    url = (f"https://github.com/daijro/camoufox/releases/download/v{version}/"
           f"camoufox-{version}-lin.{arch}.zip")
    with tempfile.TemporaryDirectory() as temporary:
        archive = Path(temporary) / "browser.zip"
        urllib.request.urlretrieve(url, archive)
        with archive.open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != expected:
                raise RuntimeError("Camoufox archive checksum mismatch")
        target = Path(destination).resolve()
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                if not (target / member.filename).resolve().is_relative_to(target):
                    raise RuntimeError("Invalid browser archive path")
            bundle.extractall(target)
            for member in bundle.infolist():
                path = target / member.filename
                if path.is_file() and (member.external_attr >> 16) & 0o111:
                    path.chmod(0o755)


if __name__ == "__main__":
    main()
