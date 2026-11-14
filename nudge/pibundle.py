"""pack the building data the pi needs (cache/ is gitignored) and unpack it there.

laptop: python -m nudge.pibundle                -> pi_bundle.tar.gz (scans, doorways, dossiers, osm, vexcel)
pi:     python -m nudge.pibundle --unpack pi_bundle.tar.gz

leaves out what the pi can re-download with keys and doesn't need to navigate: point clouds, panorama
images, the costar kit, and the live hub state.
"""
import sys
import tarfile
from pathlib import Path

from .mp import ROOT

CACHE = ROOT / "cache"
KEEP = ("models", "doorways", "dossiers", "osm", "vexcel", "replays", "demo_facade_MgGNVWsyXtH.jpg")
SKIP_SUFFIX = (".old.json",)


def pack(out=ROOT / "pi_bundle.tar.gz"):
    n = 0
    with tarfile.open(out, "w:gz") as tar:
        for name in KEEP:
            p = CACHE / name
            if not p.exists():
                continue
            for f in ([p] if p.is_file() else sorted(x for x in p.rglob("*") if x.is_file())):
                if f.name.endswith(SKIP_SUFFIX):
                    continue
                tar.add(f, arcname=str(f.relative_to(ROOT)))
                n += 1
    print(f"wrote {out} ({n} files, {out.stat().st_size / 1e6:.1f} MB)")
    return out


def unpack(path):
    with tarfile.open(path, "r:gz") as tar:
        members = [m for m in tar.getmembers() if m.name.startswith("cache/") and ".." not in m.name]
        tar.extractall(ROOT, members=members)
    print(f"unpacked {len(members)} files into {CACHE}")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--unpack":
        unpack(sys.argv[2])
    else:
        pack()
