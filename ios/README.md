# Nudge for iOS

Two SwiftUI apps in one Xcode project:

- **Nudge**: the traveller's app. Worn on the back, voice only.
- **Nudge Care**: the caretaker's app. Live status, map, help alerts, send a trip.

Both talk to the Nudge server on the Raspberry Pi over HTTP.

## Build

Requires a Mac with Xcode 15+.

```bash
brew install xcodegen
cd ios
xcodegen
open Nudge.xcodeproj
```

Or open the checked-in `Nudge.xcodeproj` directly.

## Sign and run

1. Select the **Nudge** or **NudgeCare** target, then Signing & Capabilities, and choose your team. Change the bundle id if it's taken.
2. Connect the iPhone and select it as the destination.
3. On the iPhone, enable Settings > Privacy & Security > Developer Mode.
4. Run. On first launch, trust the developer under Settings > General > VPN & Device Management.
5. Allow the permissions when asked.

Tests: ⌘U on the Nudge scheme, on a simulator.

## Use

1. The Pi, the traveller's phone and the caretaker's phone share the traveller's hotspot. Start the server on the Pi (see `docs/live-demo.md`).
2. **Nudge:** enter the Pi address, choose how the phone is worn, set step length, and note the code. Long-press the screen for 2 s to reopen setup.
3. **Nudge Care:** set the same Pi address in Settings, enter the code, then Send Trip.

## Check before the demo

Wear the phone, face a known direction, and compare with a compass app. If it's 180° off, switch the Wear setting.
