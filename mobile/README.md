# Nudge mobile

Expo (SDK 58) + TypeScript app for blind and low-vision travellers. It talks to the Nudge server
(`nudge/server.py`) for routes and live guidance, speaks every instruction, plays the haptic cues, and relays
the live line to the haptic band. All distances are in metres.

## Run

```bash
cd mobile
npm install
npx expo start          # scan the QR with Expo Go
```

Start the server on the laptop so the phone can reach it on the LAN:

```bash
python -m uvicorn nudge.server:app --host 0.0.0.0 --port 8765
```

In the app, open **Settings**, set the server to `http://<laptop-lan-ip>:8765` and tap **Save and test**. Then
**Where to**: building `synthetic`, **Load**, pick a destination, **Start**, and on **Navigate** tap
**Walk the route** for the simulated walk.

Web build (desktop browser, no phone):

```bash
npx expo export --platform web     # writes dist/
```

Serve `dist/` on the same origin as the API (or enable CORS, below); the app uses its own origin as the
server when it's served from anywhere but the Expo dev server.

## What works where

| Feature | Expo Go | Dev build | Web |
|---|---|---|---|
| Routes, live guidance, problem reports, approvals | yes | yes | yes |
| Speech (expo-speech; screen-reader announcements when VoiceOver/TalkBack is on) | yes | yes | yes |
| Haptics (Android: Vibration patterns; iOS: timed impacts) | yes | yes | navigator.vibrate where supported |
| Steps + compass (experimental) | iOS only (Android Expo Go lacks ACTIVITY_RECOGNITION) | yes | no |
| Haptic band over Bluetooth (react-native-ble-plx) | no | yes | no (use `web/companion.html` on Android Chrome) |
| Simulated band (two motor indicators, same grammar as the firmware) | yes | yes | yes |

Dev build for Bluetooth and Android step counting:

```bash
npx expo run:android              # or: npx eas-cli@latest build --profile development
```

## Accessibility

- Every control has a label, role and hint; selected options are spelled out ("✓ blind"), never colour alone.
- Targets are at least 64 dp; dark theme with yellow-on-black accents (about 14:1 contrast).
- The current instruction lives in a live region and is spoken. With a screen reader running, the app hands
  speech to VoiceOver/TalkBack (`announceForAccessibility`) instead of talking over it.
- Problem reports and reroute approvals are modal sheets with large Approve / Reject buttons.

## Server endpoints used

- `GET /api/profiles`, `GET /api/graph/{sid}`
- `POST /api/nav/start`, `POST /api/nav/update`, `POST /api/nav/block`
- `POST /api/nav/exception`, `POST /api/nav/exception/decide`: the exception agent can take ~30 s on free
  models, so the app waits up to 90 s and says "finding another way" meanwhile
- `GET /api/device/{session}/route`, `GET /api/device/{session}/live`, `POST /api/device/{session}/button`

CORS: needed only when the web build is served from a different origin than the API (for example the Expo dev
server on :8081). Native apps don't need it. Add to `nudge/server.py`:

```python
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
```

## Code

- `src/lib/device.ts`: band line protocol (mirrors `nudge/device.py`), ble UUIDs, base64
- `src/lib/haptics.ts`: band grammar engine (mirrors `web/companion.html` / firmware) and phone patterns
- `src/lib/sim.ts`: simulated walker; `src/lib/pdr.ts`: step-and-heading
- `src/lib/api.ts`: typed server client; `src/lib/band.ts`: Bluetooth link; `src/lib/voice.ts`: speech + haptics
- `src/screens/*`: Where to, Navigate, Band, Settings

Tests (`npm test`) check the protocol against real lines produced by the Python encoder
(`src/lib/__tests__/fixtures/device_lines.json`), the haptic grammar, the simulated walk and the helpers.
