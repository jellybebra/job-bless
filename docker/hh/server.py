"""Generate one stable Camoufox fingerprint and start the Playwright server."""

import json
import os
from pathlib import Path

from camoufox.addons import DefaultAddons
from camoufox.utils import launch_options
from playwright._impl._driver import compute_driver_executable

os.umask(0o077)
fingerprint = Path("/data/profile/fingerprint.json")
config = json.loads(fingerprint.read_text()) if fingerprint.exists() else {}
options = launch_options(
    executable_path="/opt/camoufox/camoufox-bin", ff_version=152, headless=False,
    os="linux", window=(1280, 800), config=config,
    exclude_addons=list(DefaultAddons),  # No runtime extension downloads.
    i_know_what_im_doing=True,
)
if not fingerprint.exists():
    chunks = sorted((int(key.rsplit("_", 1)[1]), value) for key, value in options["env"].items()
                    if key.startswith("CAMOU_CONFIG_"))
    pending = fingerprint.with_suffix(".pending")
    pending.write_text("".join(value for _, value in chunks), encoding="utf-8")
    pending.replace(fingerprint)

# Only the fingerprint is persistent. Inherited environment and launch options
# go into a private temporary file, removed by Node immediately after reading.
camel = lambda key: key.split("_")[0] + "".join(part.title() for part in key.split("_")[1:])
payload = Path("/tmp/hh-launch.json")
payload.write_text(json.dumps({camel(key): value for key, value in options.items()}))
node, driver = compute_driver_executable()
os.execv(str(node), [str(node), "/app/server.cjs", str(Path(driver).parent), str(payload)])
