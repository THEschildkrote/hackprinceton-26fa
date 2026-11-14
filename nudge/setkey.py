"""type secrets into .env without them showing on screen or in shell history.

run: python -m nudge.setkey                 (asks for PHOTON_API_KEY)
     python -m nudge.setkey VEXCEL_API_KEY MATTERPORT_API_KEY ...
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


def main(names):
    for name in names or ["PHOTON_API_KEY"]:
        value = getpass.getpass(f"{name} (input hidden, enter to skip): ").strip().strip('"').strip("'")
        if not value:
            print(f"  skipped {name}")
            continue
        save(name, value)
        print(f"  saved {name} to .env ({len(value)} characters)")


if __name__ == "__main__":
    main(sys.argv[1:])
