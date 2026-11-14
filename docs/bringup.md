# Bring-up: keys pasted to real demo

1. Enter keys without echo: `python -m nudge.setkey MATTERPORT_API_KEY MATTERPORT_API_TOKEN MATTERPORT_SDK_KEY VEXCEL_API_KEY COSTAR_KIT_URL COSTAR_ZIP_PASSWORD`. Type the zip password rather than pasting it, as the track tips say.
2. Run `python -m nudge.bringup`. It picks the best CoStar + splat building; pass `--sid <sid>` to choose one yourself.
3. Each line reads `[pass]`, `[fail]` or `[skip]` with the reason. The full report is in `cache/bringup/<sid>.json`.
4. **probe** answers the schema questions: the pano floor/room/rotation fields and the edge types. If it prints `add [...] to walkgraph.PASSABLE`, do that in `nudge/walkgraph.py`.
5. **fetch** builds the walk graph. If panos are unconnected, the door edges are typed differently than expected; the fix is printed.
6. **calibrate** writes 4 views of a corridor-like pano to `cache/bringup/`, with the expected wall distance per yaw. If no offset is suggested, compare them by eye and set `FACE1_YAW_DEG` in `nudge/perceive.py`.
7. **georef** places the scan on the Vexcel footprint. Check `cache/bringup/<sid>_georef.jpg`: a yellow outline over the roof, with cyan entrance candidates.
8. **costar** downloads, decrypts and indexes the kit once, then prints the accessibility facts (year built, floors, elevators, parking, tenants).
9. `--dossier` also runs the route-scoped quick survey with the deep agent (needs `OPENROUTER_API_KEY` or `ANTHROPIC_API_KEY`); `--goal "Suite 400"` picks the destination.
10. Finish with the printed next commands: the server, `tour.html?sid=…`, `dossier.html?sid=…`, and the Photon bot.
