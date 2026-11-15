"""draws docs/system-design.svg: what runs before the walk, the isaac api, and the devices and people around it.

    python tools/system_design.py            # writes docs/system-design.svg
    python tools/system_design.py --png      # and docs/system-design.png at 2x, through headless edge or chrome

edit the boxes below when the backend changes; the layout lines up rows across the api and device columns.
"""
import argparse
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
W, H = 1860, 1440
SANS = "Segoe UI, -apple-system, Helvetica, Arial, sans-serif"
MONO = "Consolas, Menlo, monospace"
NAVY, GREY, YELLOW = "#0a1f5c", "#4f5b73", "#fcd116"
PANEL, PANEL_LINE, BOX_LINE = "#f3f6fb", "#dfe5ef", "#d9e0ec"
SUB_FILL, SUB_LINE, SUB_TITLE = "#eef3fb", "#c9d6ee", "#1d3f9a"

out = []


def t(x, y, s, size=12.5, weight=400, color=GREY, anchor="start", font=SANS, extra=""):
    out.append(f'<text x="{x}" y="{y}" font-family="{font}" font-size="{size}" font-weight="{weight}" '
               f'fill="{color}" text-anchor="{anchor}" {extra}>{escape(s)}</text>')


def r(x, y, w, h, fill="#fff", stroke=BOX_LINE, rx=10, dash=False, sw=1):
    d = ' stroke-dasharray="6 5"' if dash else ""
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" stroke="{stroke}" '
               f'stroke-width="{sw}"{d}/>')


def pill(x_right, y, label, fill=YELLOW, color=NAVY):
    """a pill whose right edge is x_right; returns its left edge."""
    w = len(label) * 6.5 + 18
    r(x_right - w, y, w, 20, fill=fill, stroke="none", rx=10)
    t(x_right - w / 2, y + 14, label, 11, 700, color, "middle")
    return x_right - w


def arrow(pts, label=None, lx=None, ly=None, both=False, label_anchor="middle", vertical=False):
    d = "M" + " L".join(f"{x},{y}" for x, y in pts)
    start = ' marker-start="url(#ah)"' if both else ""
    out.append(f'<path d="{d}" fill="none" stroke="{NAVY}" stroke-width="1.6" marker-end="url(#ah)"{start}/>')
    if label:
        if vertical:
            t(lx, ly, label, 12, 400, GREY, "middle", extra=f'transform="rotate(-90 {lx} {ly})"')
        else:
            t(lx, ly, label, 12, 400, GREY, label_anchor)


def box(x, y, w, h, title, lines=(), mono=None, sponsor=None, dash=False, fill="#fff", stroke=BOX_LINE,
        mono_bottom=False, title_size=15):
    r(x, y, w, h, fill=fill, stroke=stroke, dash=dash)
    t(x + 18, y + 30, title, title_size, 700, NAVY)
    if sponsor:
        pill(x + w - 16, y + 15, sponsor)
    if mono and not mono_bottom:
        t(x + w - 18, y + 30, mono, 11.5, 400, GREY, "end", MONO)
    for i, line in enumerate(lines):
        t(x + 18, y + 54 + 20 * i, line)
    if mono and mono_bottom:
        t(x + w - 18, y + h - 14, mono, 11.5, 400, GREY, "end", MONO)


def need(n):
    """height of a box with a title and n lines."""
    return 44 + 20 * n + 16 if n else 56


def draw():
    out.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">')
    out.append(f'<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
               f'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{NAVY}"/></marker></defs>')
    r(0, 0, W, H, fill="#fff", stroke="none", rx=0)

    # header and legend
    t(40, 58, "Isaac system design", 26, 700, NAVY)
    t(318, 58, "Real-time indoor guidance for blind travellers", 15)
    left = pill(1560, 40, "Sponsor")
    t(1572, 54, "sponsor technology")
    arrow([(1700, 50), (1740, 50)])
    t(1748, 54, "data and calls")

    # columns
    c1, c2, c3 = (30, 610), (700, 560), (1300, 530)
    top, bottom = 100, 1376
    for (x, w), title, sub in ((c1, "Before the walk", "once per building, ahead of time"),
                               (c2, "Isaac API", "FastAPI, one process: on the Pi or a laptop behind a Cloudflare "
                                                 "tunnel, or a cloud host"),
                               (c3, "Devices and people", "everyone talks to the same API over HTTPS")):
        r(x, top, w, bottom - top, fill=PANEL, stroke=PANEL_LINE, rx=16)
        t(x + 24, top + 38, title, 18, 700, NAVY)
        t(x + 24, top + 60, sub)

    # ---- column 1: sources and the building model
    sx, sw_ = 54, 262
    srcs = [("Matterport", ["Model API: floors, walls, rooms", "Point cloud and 360° panoramas"], "Sponsor", False),
            ("Vexcel", ["Footprints and oblique facades", "Crosswalks, curbs and slope outside"], "Sponsor", False),
            ("CoStar", ["Year built and renovated, pre-ADA,", "floors, parking and tenants"], "Sponsor", False),
            ("OpenStreetMap + USGS", ["Footways, crossings, elevation", "where Vexcel's outline doesn't fit"],
             None, True)]
    y = 180
    for title, lines, sp, dash in srcs:
        box(sx, y, sw_, 104, title, lines, sponsor=sp, dash=dash)
        arrow([(sx + sw_, y + 52), (sx + sw_ + 34, y + 52)])
        y += 120
    bx, bw = 354, 262
    r(bx, 180, bw, 464)
    t(bx + 18, 210, "Building model", 15, 700, NAVY)
    t(bx + 18, 230, "walkgraph.py, georef.py, outdoor.py", 11.5, 400, GREY, font=MONO)
    rows = [("Walk graph", "scan positions become nodes"), ("Doorways", "found in the point cloud"),
            ("Floors", "stairs and elevators joined"), ("Street position", "scan lined up with the footprint"),
            ("Outdoor leg", "footpath, crossings, the entrance"), ("Places", "the venue's own names for rooms")]
    for i, (a, b) in enumerate(rows):
        yy = 262 + i * 64
        if i:
            out.append(f'<path d="M{bx + 18},{yy - 22} L{bx + bw - 18},{yy - 22}" stroke="{BOX_LINE}"/>')
        t(bx + 18, yy, a, 13.5, 700, NAVY)
        t(bx + 18, yy + 20, b, 12.5)

    # ---- column 1: the survey agent and the hazard pass
    ax, ay, aw, ah = 54, 692, 562, 660
    arrow([(bx + 60, 644), (bx + 60, ay)], "scan data", bx + 72, 672, label_anchor="start")
    arrow([(bx + bw - 30, ay), (bx + bw - 30, 644)], "dossier", bx + bw - 42, 672, label_anchor="end")
    r(ax, ay, aw, ah)
    t(ax + 20, ay + 32, "Survey agent", 18, 700, NAVY)
    pill(ax + 20 + 128 + 168, ay + 15, "LangChain Deep Agents")
    t(ax + 20, ay + 56, "writes the building dossier; model: Gemini via LangChain")
    r(ax + 20, ay + 72, aw - 40, 36, fill=NAVY, stroke="none", rx=8)
    t(ax + 38, ay + 96, "Lead plans the survey, delegates, then audits its own routes", 13.5, 600, "#fff")
    subs = [[("place-indexer", "names every section"), ("hazard-scout", "judges each hazard lead"),
             ("landmark-writer", "touch, sound, underfoot")],
            [("site-surveyor", "the approach, from aerial and CoStar"),
             ("building-cartographer", "joins buildings made of several scans")]]
    yy = ay + 122
    for row in subs:
        n = len(row)
        cw = (aw - 40 - 12 * (n - 1)) / n
        for i, (a, b) in enumerate(row):
            xx = ax + 20 + i * (cw + 12)
            r(xx, yy, cw, 58, fill=SUB_FILL, stroke=SUB_LINE, rx=8)
            t(xx + 14, yy + 24, a, 13.5, 700, SUB_TITLE)
            t(xx + 14, yy + 44, b, 12)
        yy += 70

    # the hazard pass
    hx, hy, hw, hh = ax + 20, yy + 6, aw - 40, 196
    r(hx, hy, hw, hh, fill="#fffbe6", stroke=YELLOW, rx=10, sw=1.5)
    t(hx + 16, hy + 28, "Hazard pass: every point-cloud lead gets a verdict", 14.5, 700, NAVY)
    t(hx + hw - 16, hy + 28, "hazards.py", 11.5, 400, GREY, "end", MONO)
    t(hx + 16, hy + 50, "Code walks every lead, so the model can't skip one; the hazard-scout")
    t(hx + 16, hy + 69, "judges each from a view rendered at it, stricter if CoStar says pre-ADA.")
    chips = ["lead", "rendered view", "Verdict", "cane rule", "evidence"]
    notes = ["overhang or glass", "from the nearest scan", "hazard / not / can't tell",
             "furniture never counts", "the view, kept"]
    widths = [len(c) * 7 + 20 for c in chips]
    gap = (hw - 32 - sum(widths)) / (len(chips) - 1)
    xx, spans = hx + 16, []
    for i, (c, cw) in enumerate(zip(chips, widths)):
        r(xx, hy + 84, cw, 30, fill="#fff", stroke=YELLOW, rx=15)
        t(xx + cw / 2, hy + 104, c, 12.5, 700, NAVY, "middle")
        if i:
            arrow([(xx - gap + 3, hy + 99), (xx - 3, hy + 99)])
        spans.append((xx, xx + cw))
        xx += cw + gap
    for i, (nt, (a, b)) in enumerate(zip(notes, spans)):  # staggered; the end ones hug the box's inside edges
        x, anchor = (a, "start") if i == 0 else (b, "end") if i == len(notes) - 1 else ((a + b) / 2, "middle")
        t(x, hy + 134 + (i % 2) * 16, nt, 11, 400, GREY, anchor)
    t(hx + 16, hy + 182, "At JRR: 55 leads judged, 32 confirmed (26 glass, 6 overhead), 23 rejected.", 12.5, 700,
      NAVY)

    yy = hy + hh + 26
    t(ax + 20, yy, "Tools: look at a panorama, find overhangs and glass, aerial view,")
    t(ax + 20, yy + 19, "CoStar record, audit the routes")
    fy = yy + 36
    files = [("/dossier", "the router reads it"), ("/memories", "lessons, kept by the run"),
             ("/agent", "read-only skills")]
    fw = (aw - 40 - 24) / 3
    for i, (a, b) in enumerate(files):
        xx = ax + 20 + i * (fw + 12)
        r(xx, fy, fw, 58, fill=PANEL, stroke=BOX_LINE, rx=8)
        t(xx + 14, fy + 24, a, 13, 700, NAVY, font=MONO)
        t(xx + 14, fy + 44, b, 12)
    t(ax + 20, fy + 84, "briefing.md is read to the traveller before the visit; audit.md says what was")
    t(ax + 20, fy + 103, "checked and confirmed, and what is still open.")

    # ---- column 2 and 3, row by row so the arrows run straight across
    x2, w2 = 736, 500
    x3, w3 = 1340, 466
    gx0, gx1 = x2 + w2, x3  # the gutter the cross-column arrows run in
    mid = (gx0 + gx1) / 2

    def across(y, label, to_api=False, both=False):
        pts = [(gx1, y), (gx0, y)] if to_api else [(gx0, y), (gx1, y)]
        arrow(pts, label, mid, y - 7, both=both)

    # row 1: stall watch and tracker
    y = 180
    box(x2, y, 242, 104, "Stall watch", ["stopped, backing up, shuffling:", "“Is something in the way?”"],
        "stalls.py")
    box(x2 + 258, y, 242, 104, "Tracker", ["steps + compass become", "a position on the route"], "pdr.py")
    arrow([(x2 + 258 + 121, y + 104), (x2 + 258 + 121, y + 120)])
    # iPhone spans rows 1-3
    box(x3, y, w3, 368, "Traveller's iPhone", ["Isaac app, worn on the back", "steps and compass from the motion sensors",
                                              "speech in, spoken directions out", "tap to call Isaac or to talk",
                                              "the source of every position"], "ios/Nudge", mono_bottom=True)
    across(y + 60, "steps, compass", to_api=True)

    # row 2: navigator
    y = 300
    box(x2, y, w2, 124, "Navigator", ["A* for blind and wheelchair profiles",
                                      "“12 meters, then left”: a distance and a direction,",
                                      "hazards by height and side, never what things look like"], "navigator.py")
    across(y + 62, "directions")
    arrow([(bx + bw, 362), (x2, 362)], "walk graph", (bx + bw + x2) / 2, 355)

    # row 3: voice brain (two arrows in: the app and a phone call)
    y = 440
    box(x2, y, w2, 208, "Voice brain", ["“Take me to the seminar room”, where am I,", "how far, help, stop",
                                        "one endpoint for the app, the browser and calls:",
                                        "POST /api/voice/turn, /api/voice/call"], "voice.py")
    across(y + 68, "voice", both=True)
    box(x3, y + 124, w3, 104, "Phone call", ["Photon number → SIP → ElevenLabs agent;",
                                             "its webhook posts to /api/voice/call"], "tools/phone_line.py",
        sponsor="Sponsor", mono_bottom=True)
    across(y + 176, "call", to_api=True)

    # row 4: belt
    y = 684
    box(x2, y, w2, 104, "Belt link", ["hands the route to the belt, reads back where", "the wearer is; next floor, stop"],
        "belt.py")
    box(x3, y, w3, 104, "Belt", ["Raspberry Pi + two servos on a PCA9685 board:", "keeps the position, taps each turn"],
        "NUDGE_BELT_URL")
    across(y + 52, "route, position", both=True)

    # row 5: caretaker hub
    y = 804
    box(x2, y, w2, 104, "Caretaker hub", ["pairing codes, live status, trips, help alerts;",
                                          "knows whether the phone and the caretaker are online"], "hub.py")
    box(x3, y, w3, 104, "Isaac Care app", ["the caretaker's live map;", "sends trips, texts questions"], "ios/NudgeCare",
        mono_bottom=True)
    across(y + 52, "pair, status", both=True)

    # row 6: iMessage
    y = 924
    box(x2, y, w2, 104, "iMessage bot", ["caretakers and the walker, by text;",
                                         "polls Photon, so it needs no public URL"], "photon_bot.py")
    box(x3, y, w3, 104, "iMessage", ["status, help alerts and trips by text,", "for caretakers and the walker"],
        sponsor="Sponsor")
    across(y + 52, "texts", both=True)

    # row 7: traces and the web pages
    y = 1044
    box(x2, y, w2, 84, "Trace bus", ["every event and agent call, streamed live"], "traces.py")
    box(x3, y, w3, 84, "Web", ["ops dashboard (sign-in), caretaker page, venue routes"], "web/")
    across(y + 42, "events")

    # row 8: live agents and the model
    y = 1144
    r(x2, y, w2, 208)
    t(x2 + 18, y + 32, "Live agents", 18, 700, NAVY)
    pill(x2 + 18 + 108 + 168, y + 15, "LangChain Deep Agents")
    t(x2 + 18, y + 56, "only when something goes wrong; routing never waits on a model")
    lw = (w2 - 36 - 12) / 2
    for i, (a, l1, l2) in enumerate((("Exception agent", "locked door, blocked hall:", "compares routes, proposes one"),
                                     ("Explorer agent", "outside the scan:", "plans the way with the traveller"))):
        xx = x2 + 18 + i * (lw + 12)
        r(xx, y + 70, lw, 76, fill=SUB_FILL, stroke=SUB_LINE, rx=8)
        t(xx + 14, y + 96, a, 14, 700, SUB_TITLE)
        t(xx + 14, y + 116, l1, 12)
        t(xx + 14, y + 134, l2, 12)
    r(x2 + 18, y + 158, w2 - 36, 34, fill=YELLOW, stroke="none", rx=8)
    t(x2 + w2 / 2, y + 180, "The traveller says yes before any reroute", 14, 700, NAVY, "middle")
    t(x2 + w2 - 18, y + 32, "exception.py, explorer.py", 11.5, 400, GREY, "end", MONO)
    # reroute after approval: back up to the navigator
    arrow([(x2, y + 108), (x2 - 18, y + 108), (x2 - 18, 400), (x2, 400)],
          "reroute after approval", x2 - 26, (y + 108 + 400) / 2, vertical=True)
    t(x3, y + 6, "Model", 13, 700, GREY)
    box(x3, y + 20, w3, 124, "Gemini 3.5 Flash Lite", ["the agents' model, through LangChain",
                                                       "every survey, verdict and reroute",
                                                       "Claude or OpenRouter as fallback"], "agents/llm.py",
        mono_bottom=True)
    across(y + 82, "LangChain", both=True)

    t(40, H - 34, "Routing, tracking and taps are deterministic. The models survey buildings ahead of time, judge "
                  "every hazard lead with evidence, and handle exceptions; they are never in the loop of a step.", 13)
    out.append("</svg>")
    return "\n".join(out) + "\n"


def png(svg_path, png_path):
    exe = next((p for p in (shutil.which("msedge"), shutil.which("chrome"),
                            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                            r"C:\Program Files\Google\Chrome\Application\chrome.exe") if p and os.path.exists(p)), None)
    if not exe:
        raise SystemExit("no edge or chrome found for the png; the svg is written")
    # a throwaway profile: with the browser already open, a headless call is handed to it and writes nothing.
    # even so it now and then exits without a file, so try a few times
    for _ in range(3):
        # the browser's helpers can hold the profile open a moment after the screenshot; leave it to the os then
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as profile:
            subprocess.run([exe, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                            "--force-device-scale-factor=2", f"--user-data-dir={profile}", f"--window-size={W},{H}",
                            f"--screenshot={png_path}", svg_path.resolve().as_uri()],
                           check=True, capture_output=True, timeout=120)
            for _ in range(10):
                if Path(png_path).exists():
                    return
                time.sleep(0.5)
    raise SystemExit(f"the browser wrote no png at {png_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--png", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "docs" / "system-design.svg"))
    a = ap.parse_args()
    svg = Path(a.out)
    svg.write_text(draw(), encoding="utf-8")
    print(f"wrote {svg}")
    if a.png:
        png(svg, svg.with_suffix(".png"))
        print(f"wrote {svg.with_suffix('.png')}")
