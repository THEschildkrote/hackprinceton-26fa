"""type secrets into .env without them showing on screen or in shell history.

run: python -m nudge.setkey                 (asks for PHOTON_API_KEY)
     python -m nudge.setkey VEXCEL_API_KEY MATTERPORT_API_KEY ...
     python -m nudge.setkey --clipboard ELEVENLABS_API_KEY   (takes what was just copied; never shown)
"""
import getpass
import sys

from .mp import ROOT

ENV = ROOT / ".env"


def save(name, value):
    lines = ENV.read_text(encoding="utf-8").splitlines() if ENV.exists() else []
    for i, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == name:
            lines[i] = f"{name}={value}"
            break
    else:
        lines.append(f"{name}={value}")
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def clipboard():
    import subprocess
    r = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"], capture_output=True, text=True)
    return (r.stdout or "").strip().strip('"').strip("'")


def main(names):
    if names and names[0] == "--clipboard":
        for name in names[1:]:
            value = clipboard()
            if not value or len(value) < 20 or " " in value:
                raise SystemExit(f"the clipboard doesn't hold a key for {name}")
            save(name, value)
            print(f"  saved {name} to .env from the clipboard ({len(value)} characters)")
        return
    for name in names or ["PHOTON_API_KEY"]:
        value = getpass.getpass(f"{name} (input hidden, enter to skip): ").strip().strip('"').strip("'")
        if not value:
            print(f"  skipped {name}")
            continue
        save(name, value)
        print(f"  saved {name} to .env ({len(value)} characters)")


if __name__ == "__main__":
    main(sys.argv[1:])
