"""Real Altium files for the import tests, fetched once and cached.

The files come from KiCad's own importer test data and from sinara-hw/Sampler
(CERN-OHL), pinned by commit and SHA-256. Their licences do not sit well in an
MIT repository, so they are downloaded into tests/.altium_cache/ (gitignored)
instead of vendored. Without network the tests that need them skip.
"""

from __future__ import annotations

import hashlib
import os
import urllib.request
from pathlib import Path

import pytest

CACHE = Path(__file__).parent / ".altium_cache"

#: name -> (url, sha256)
FIXTURES: dict[str, tuple[str, str]] = {
    "eDP/eDP_adapter_dvt1.PcbDoc": (
        "https://gitlab.com/kicad/code/kicad/-/raw/de08734017d03e0a0abd6f0f1aeef1772782f708/qa/data/pcbnew/plugins/altium/eDP_adapter_dvt1_source/eDP_adapter_dvt1.PcbDoc",
        "17896fdfeaac33a84ac3063db063d0a4d211c127c997632c8000837c0ce6fc12"),
    "eDP/it6251_core.SchDoc": (
        "https://gitlab.com/kicad/code/kicad/-/raw/de08734017d03e0a0abd6f0f1aeef1772782f708/qa/data/pcbnew/plugins/altium/eDP_adapter_dvt1_source/it6251_core.SchDoc",
        "68fb75264814e45985f94b6a2c7c0268a04e46dabebe098c2196a7026b847172"),
    "eDP/power.SchDoc": (
        "https://gitlab.com/kicad/code/kicad/-/raw/de08734017d03e0a0abd6f0f1aeef1772782f708/qa/data/pcbnew/plugins/altium/eDP_adapter_dvt1_source/power.SchDoc",
        "8c5f70db1f3cb81f43857fb1dfe7db251e0124d2aae3803cbb98ddbb27e81cab"),
    "eDP/eDP_adapter_dvt1.PrjPcb": (
        "https://gitlab.com/kicad/code/kicad/-/raw/de08734017d03e0a0abd6f0f1aeef1772782f708/qa/data/pcbnew/plugins/altium/eDP_adapter_dvt1_source/eDP_adapter_dvt1.PrjPcb",
        "6f54e784dff12bba3c58f1a135da54d92107fc6cc2ca133d2f6337acb8769e26"),
    "qa/issue16903/harness_subscheet.SchDoc": (
        "https://gitlab.com/kicad/code/kicad/-/raw/de08734017d03e0a0abd6f0f1aeef1772782f708/qa/data/eeschema/plugins/altium/issue16903/harness_subscheet.SchDoc",
        "5ed75ef2afb3e48714f4c0574593e9588049ecb4387d19b00d180e8c0d925c86"),
    "qa/issue16903/hierarchical_schematic_top.SchDoc": (
        "https://gitlab.com/kicad/code/kicad/-/raw/de08734017d03e0a0abd6f0f1aeef1772782f708/qa/data/eeschema/plugins/altium/issue16903/hierarchical_schematic_top.SchDoc",
        "2fc3b0464041d772a556c62e1d1e4dac19890a4bb0af2275704a4910abe6b29e"),
    "qa/issue16903/sub_sheet.SchDoc": (
        "https://gitlab.com/kicad/code/kicad/-/raw/de08734017d03e0a0abd6f0f1aeef1772782f708/qa/data/eeschema/plugins/altium/issue16903/sub_sheet.SchDoc",
        "d11ac86cb5980cab9ad7135c34d5ec651f8dcf8f0f3e6a277edc6637f6181d0b"),
    "qa/issue24861/Channel.SchDoc": (
        "https://gitlab.com/kicad/code/kicad/-/raw/de08734017d03e0a0abd6f0f1aeef1772782f708/qa/data/eeschema/plugins/altium/issue24861/Channel.SchDoc",
        "e7dc42a4e983e6b716f6883eb0863fe47164d941636e11b1f2b5ec5d0143dca4"),
    "qa/issue24861/Repeated_Schematic.PrjPcb": (
        "https://gitlab.com/kicad/code/kicad/-/raw/de08734017d03e0a0abd6f0f1aeef1772782f708/qa/data/eeschema/plugins/altium/issue24861/Repeated_Schematic.PrjPcb",
        "229c3da4b633b952b0051bcef98db3afab0699970ba9537fa0bb4d7114eaf7c2"),
    "qa/issue24861/Repeated_Schematic.SchDoc": (
        "https://gitlab.com/kicad/code/kicad/-/raw/de08734017d03e0a0abd6f0f1aeef1772782f708/qa/data/eeschema/plugins/altium/issue24861/Repeated_Schematic.SchDoc",
        "7bee25094f209e4e412faf005efb73ac4ab543703b7a40d794bc96b84109827f"),
    "qa/ticket1303/overview.SchDoc": (
        "https://gitlab.com/kicad/code/kicad/-/raw/de08734017d03e0a0abd6f0f1aeef1772782f708/qa/data/eeschema/plugins/altium/ticket1303/overview.SchDoc",
        "af9fe7044e97a48af92d45de125b2671341dae541e64ca5bcb964dae8c632b17"),
    "qa/ticket1303/pagea.SchDoc": (
        "https://gitlab.com/kicad/code/kicad/-/raw/de08734017d03e0a0abd6f0f1aeef1772782f708/qa/data/eeschema/plugins/altium/ticket1303/pagea.SchDoc",
        "075dbd9a3984371ef11b3d9c645219c9da41b557bb7d9599f99f8ca1d48f73ff"),
    "qa/ticket1303/pageb.SchDoc": (
        "https://gitlab.com/kicad/code/kicad/-/raw/de08734017d03e0a0abd6f0f1aeef1772782f708/qa/data/eeschema/plugins/altium/ticket1303/pageb.SchDoc",
        "869e8cd3af3c9abcd2ed08e6557d9dfb2ebf1eca79df2638687b96ed0f0ab967"),
    "qa/issue22943/1_cover.SchDoc": (
        "https://gitlab.com/kicad/code/kicad/-/raw/de08734017d03e0a0abd6f0f1aeef1772782f708/qa/data/eeschema/plugins/altium/issue22943/1_cover.SchDoc",
        "0f85f3d737971ef74de1d76d6d6be13e96796bdf91b9546d995870ed873e1886"),
    "sampler/ADC_8CH.SchDoc": (
        "https://raw.githubusercontent.com/sinara-hw/Sampler/f115a2ae670323c378163ce1fce2b3975aab71b4/PCB/ADC_8CH.SchDoc",
        "a1e080747bd7384d09e9a2bb7e9e851a939778d066a7e2aa33a3579e3db92e36"),
    "sampler/Input_channel.SchDoc": (
        "https://raw.githubusercontent.com/sinara-hw/Sampler/f115a2ae670323c378163ce1fce2b3975aab71b4/PCB/Input_channel.SchDoc",
        "5be941cae2ee30c6d9c995cf99582c3f487e655e485578716334ac8fb22075ff"),
    "sampler/LVDS_IFCA.SchDoc": (
        "https://raw.githubusercontent.com/sinara-hw/Sampler/f115a2ae670323c378163ce1fce2b3975aab71b4/PCB/LVDS_IFCA.SchDoc",
        "23ac4501bb9021176e58a6cc446c660902c999f764b52a131e0827cc534b9326"),
    "sampler/LVDS_IFCB.SchDoc": (
        "https://raw.githubusercontent.com/sinara-hw/Sampler/f115a2ae670323c378163ce1fce2b3975aab71b4/PCB/LVDS_IFCB.SchDoc",
        "386f2bbcbc57240901f997cb4db29186035555f74c4b4e9a1b7e0618b27bf34f"),
    "sampler/Sampler.PCBDOC": (
        "https://raw.githubusercontent.com/sinara-hw/Sampler/f115a2ae670323c378163ce1fce2b3975aab71b4/PCB/Sampler.PCBDOC",
        "b185f0834ec2903358a4c08e95f41ce6f9df6f971679a2b8e17bd8a26d1a69c5"),
    "sampler/Sampler.PrjPCB": (
        "https://raw.githubusercontent.com/sinara-hw/Sampler/f115a2ae670323c378163ce1fce2b3975aab71b4/PCB/Sampler.PrjPCB",
        "9b81fcd99c1ecd218c953280cea4df7259534f7007a96ee06b8b7021c7ea72d5"),
    "sampler/Sampler.schdoc": (
        "https://raw.githubusercontent.com/sinara-hw/Sampler/f115a2ae670323c378163ce1fce2b3975aab71b4/PCB/Sampler.schdoc",
        "525088e9ee1d6f0b2df9f59b4d38bfaf7cac26f77a179976f7464a18aa8b429c"),
    "sampler/Supply_Sampler.SchDoc": (
        "https://raw.githubusercontent.com/sinara-hw/Sampler/f115a2ae670323c378163ce1fce2b3975aab71b4/PCB/Supply_Sampler.SchDoc",
        "8bd98963af7cb4cabb12db7da4b35d67f6b271ebe990c7b424f23bc33a6644a4"),
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def altium_fixture(name: str) -> Path:
    """The cached file for `name`, downloading it the first time; skip if offline."""
    url, sha = FIXTURES[name]
    target = CACHE / name
    if target.is_file() and _sha256(target) == sha:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310 - pinned https URLs
            data = resp.read()
    except OSError as exc:
        pytest.skip(f"cannot fetch Altium fixture {name}: {exc}")
    if hashlib.sha256(data).hexdigest() != sha:
        pytest.fail(f"Altium fixture {name} does not match its pinned SHA-256")
    # Parallel workers may fetch the same file at once: write aside, then swap in.
    tmp = target.with_name(f"{target.name}.{os.getpid()}.part")
    tmp.write_bytes(data)
    os.replace(tmp, target)
    return target


def altium_set(prefix: str) -> list[Path]:
    """Every fixture under `prefix/`, e.g. altium_set("sampler")."""
    return [altium_fixture(n) for n in FIXTURES if n.startswith(prefix + "/")]
