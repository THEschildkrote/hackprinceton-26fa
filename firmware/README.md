# Nudge band

A wrist or belt band with two vibration motors, left and right. It steers a blind traveller using the
navigator's metres and degrees, with no words needed. The phone companion (`web/companion.html`) relays
lines from the server to the band over Bluetooth LE.

```
server (navigator)  --http-->  phone (companion.html, Web Bluetooth)  --ble-->  band (ESP32)
                    <-- POST button events ------------------------------  "B,repeat" / "B,lost"
```

## Bill of materials

| Part | Qty | Notes |
|---|---|---|
| ESP32 dev board (ESP32-WROOM / DevKitC) | 1 | any ESP32 with BLE; not the S2, which has no Bluetooth |
| Coin vibration motor, 3 V, 10 mm | 2 | one per side |
| NPN transistor (2N2222 / PN2222 / S8050) | 2 | or one DRV2605L haptic driver per motor |
| 1 kOhm resistor | 2 | transistor base |
| 1N4148 or 1N4001 diode | 2 | flyback, across each motor |
| Momentary push button | 1 | repeat / lost |
| Piezo buzzer | 1 | optional, floor-change chirp |
| LiPo + charger, or a USB power bank | 1 | |
| Wrist straps or a belt | 2 | motors about 15 cm apart if on a belt |

## Wiring

| Signal | ESP32 pin | To |
|---|---|---|
| `PIN_MOTOR_L` | GPIO 25 | 1k to the left NPN base. Emitter to GND; collector to the motor's negative lead. Motor positive to 3V3. Diode across the motor (cathode to 3V3). |
| `PIN_MOTOR_R` | GPIO 26 | same, right motor |
| `PIN_BUTTON` | GPIO 27 | button to GND (internal pull-up) |
| `PIN_PIEZO` | GPIO 14 | piezo to GND (set `USE_PIEZO = true`) |
| `PIN_LED` | GPIO 2 | on-board LED, lit while connected |

The pins are constants at the top of `nudge_band/nudge_band.ino`.

## Build and flash

- Arduino IDE: add the ESP32 board package (arduino-esp32 2.x or 3.x), open `nudge_band/nudge_band.ino`, select your ESP32 board and upload. It uses the core's built-in `BLEDevice` library, so no other libraries are needed.
- Serial monitor at 115200 prints `nudge band advertising` and any rejected lines.

## Bluetooth LE

| | UUID |
|---|---|
| Service | `7a6e0001-1d6e-4c1a-9b5e-6e7564676500` |
| RX: phone writes lines | `7a6e0002-1d6e-4c1a-9b5e-6e7564676500` (write, write without response) |
| TX: band notifies button events | `7a6e0003-1d6e-4c1a-9b5e-6e7564676500` (notify) |

The device advertises as `Nudge Band`. Each write is one ASCII line. Lines are always under 20 bytes, so
each fits one packet at the default MTU.

## Line protocol

The contract is the docstring in `nudge/device.py`. The band parses:

| Line | Example | Meaning |
|---|---|---|
| route step | `2,T,L,90,4.5` | step 2: turn left 90 deg after 4.5 m |
| floor step | `6,F,U,E,1` | step 6: up by elevator to floor index 1 |
| live | `N,2.4,-15,TR,0` | 2.4 m to the next maneuver (a right turn); steer 15 deg right now; flags 0 |
| cue (band-only extension) | `C,hazard` | play a named pattern now; the companion sends it |

Live flags (add them up): 1 = off route, 2 = arrived, 4 = unmapped (off the map; m/deg point back along
your own trail, or are empty when there's no safe way back), 8 = hazard ahead. Angles are model-frame
degrees, + = left.

Doorways arrive as extra straight route steps, `i,S,S,0,<m>`, at the middle of each opening, so the
next step's metres count from the door.

## Haptic grammar

| Situation | Feel |
|---|---|
| On course (\|deg\| <= 15) | nothing |
| Drifting: walk direction is +deg (left) / -deg (right) | 40 ms pulses on that side. The period shrinks from 1.2 s at 15 deg to 0.3 s at 90 deg or more, so faster means turn more. |
| Within 2 m of a turn | the turn cue on that side, once per maneuver: **left = 2 pulses** (left motor), **right = 3 pulses** (right motor), bear = short + long |
| U-turn ahead | long-long on both |
| Floor change ahead | three medium pulses on both (plus a chirp if the piezo is fitted: high = up, low = down) |
| Hazard (flag 8 or `C,hazard`) | rapid buzz on both, 5 x 30 ms |
| Unmapped (flag 4) | slow: three 600 ms pulses on both, repeated every 4 s while off the map; no turn taps. Steering pulses, if any, point back the way you came |
| Off route (flag 1) | one long 500 ms buzz on both |
| Arrived (flag 2) | rising: short, short, long on both; then silent |
| Button short press | sends `B,repeat`; the phone speaks the current step; a tick on both motors |
| Button long press (0.8 s) | sends `B,lost`; the server re-plans from the last position; a long buzz |

The patterns mirror `CUES` in `nudge/instruct.py`. One-shot patterns pre-empt steering pulses. All timing
is non-blocking (`millis()`); `loop()` never calls `delay()`.

## Host tests

```bash
cd firmware/test
g++ -std=c++17 -Wall -I ../nudge_band test_protocol.cpp -o test_protocol && ./test_protocol
```

These check the parser (good and malformed lines) and the haptic grammar on simulated time. Pipe real
server lines through the parser with `./test_protocol -`; it exits non-zero if any line is rejected.
