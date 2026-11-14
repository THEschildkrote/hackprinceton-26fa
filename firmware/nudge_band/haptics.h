// haptic grammar for two motors (left = bit 1, right = bit 2). pure c++; time is passed in (millis()).
// one-shot patterns (off route, arrive, hazard, turn approach, floor) pre-empt the steady steering pulses.
#pragma once
#include <stdint.h>
#include <string.h>
#include "protocol.h"

namespace nudge {

enum : uint8_t { M_L = 1, M_R = 2, M_BOTH = 3 };

struct Pulse { uint16_t on_ms; uint16_t off_ms; uint8_t motors; };
struct Pattern { Pulse p[12]; uint8_t n; };

// mirrors nudge/instruct.py CUES (ms on/off), with the side mapped to a motor
inline Pattern cue_pattern(const char* cue) {
  Pattern t; memset(&t, 0, sizeof(t));
  auto add = [&](uint16_t on, uint16_t off, uint8_t m) { if (t.n < 12) t.p[t.n++] = Pulse{on, off, m}; };
  if (!strcmp(cue, "left")) { add(120, 80, M_L); add(120, 0, M_L); }
  else if (!strcmp(cue, "right")) { add(120, 80, M_R); add(120, 80, M_R); add(120, 0, M_R); }
  else if (!strcmp(cue, "bear_left")) { add(60, 60, M_L); add(120, 0, M_L); }
  else if (!strcmp(cue, "bear_right")) { add(60, 60, M_R); add(120, 60, M_R); add(120, 0, M_R); }
  else if (!strcmp(cue, "uturn")) { add(400, 100, M_BOTH); add(400, 0, M_BOTH); }
  else if (!strcmp(cue, "floor")) { add(200, 100, M_BOTH); add(200, 100, M_BOTH); add(200, 0, M_BOTH); }
  else if (!strcmp(cue, "arrive")) { add(60, 60, M_BOTH); add(60, 60, M_BOTH); add(300, 0, M_BOTH); }
  else if (!strcmp(cue, "hazard")) { for (int i = 0; i < 5; ++i) add(30, 30, M_BOTH); }
  else if (!strcmp(cue, "off_route")) { add(500, 0, M_BOTH); }
  else if (!strcmp(cue, "unmapped")) { add(600, 400, M_BOTH); add(600, 400, M_BOTH); add(600, 0, M_BOTH); }
  else if (!strcmp(cue, "straight")) { add(60, 0, M_BOTH); }
  return t;
}

// which cue to tap as a maneuver gets close
inline const char* approach_cue(char act, char dir) {
  switch (act) {
    case 'T': return dir == 'L' ? "left" : dir == 'R' ? "right" : 0;
    case 'B': return dir == 'L' ? "bear_left" : dir == 'R' ? "bear_right" : 0;
    case 'U': return "uturn";
    case 'F': return "floor";
    default: return 0;  // straight, walk-to-elevator, arrive (arrive fires on the done flag)
  }
}

class Engine {
 public:
  static const int STEER_DEG = 15;      // dead band: on course inside +-15
  static constexpr float NEAR_M = 2.0f;  // tap the turn side inside this
  static constexpr float RESET_M = 3.0f;
  static const uint32_t UNMAPPED_REPEAT_MS = 4000;  // keep reminding while off the map

  void play(const char* cue, uint32_t now) {
    Pattern t = cue_pattern(cue);
    if (!t.n) return;
    pat_ = t; idx_ = 0; phase_on_ = true; t0_ = now; active_ = true;
    pat_unmapped_ = !strcmp(cue, "unmapped");
  }

  void on_live(const Live& l, uint32_t now) {
    live_ = l; have_live_ = true;
    if (l.off && !was_off_) play("off_route", now);
    was_off_ = l.off;
    if (l.done && !was_done_) play("arrive", now);
    was_done_ = l.done;
    if (l.hazard && !was_hazard_) play("hazard", now);
    was_hazard_ = l.hazard;
    // off the map: slow both-sides pattern; steering pulses (if any) point back along your own trail
    if (l.unmapped && !was_unmapped_) { play("unmapped", now); unmapped_t0_ = now; }
    if (!l.unmapped && was_unmapped_ && active_ && pat_unmapped_) active_ = false;  // back on the map: stop now
    was_unmapped_ = l.unmapped;
    if (l.unmapped) return;  // no turn taps: there is no route here
    // new maneuver ahead, or we backed off: allow the approach tap again
    if (l.act != last_act_ || l.dir != last_dir_ || (l.has_m && l.m_next > RESET_M)) tapped_ = false;
    last_act_ = l.act; last_dir_ = l.dir;
    if (!tapped_ && !l.done && l.has_m && l.m_next < NEAR_M) {
      const char* c = approach_cue(l.act, l.dir);
      if (c) { play(c, now); tapped_ = true; }
    }
  }

  // steering pulse period: 1200 ms at 15 deg down to 300 ms at 90+ deg
  static uint16_t steer_period(int deg) {
    int a = deg < 0 ? -deg : deg;
    if (a <= STEER_DEG) return 0;
    if (a >= 90) return 300;
    return (uint16_t)(1200 - (a - STEER_DEG) * (900 / (90 - STEER_DEG)));
  }

  // motor bitmask to drive right now
  uint8_t tick(uint32_t now) {
    if (active_) {
      for (;;) {
        const Pulse& p = pat_.p[idx_];
        uint32_t dur = phase_on_ ? p.on_ms : p.off_ms;
        if (now - t0_ < dur) return phase_on_ ? p.motors : 0;
        t0_ += dur;
        if (phase_on_) { phase_on_ = false; continue; }
        if (++idx_ >= pat_.n) { active_ = false; steer_t0_ = now; break; }
        phase_on_ = true;
      }
    }
    if (have_live_ && live_.unmapped && now - unmapped_t0_ >= UNMAPPED_REPEAT_MS) {
      unmapped_t0_ = now; play("unmapped", now); return tick(now);
    }
    if (!have_live_ || live_.done || live_.off || !live_.has_deg) return 0;
    uint16_t period = steer_period(live_.deg_next);
    if (!period) return 0;
    if (now - steer_t0_ >= period) steer_t0_ += period * ((now - steer_t0_) / period);
    return (now - steer_t0_) < 40 ? (live_.deg_next > 0 ? M_L : M_R) : 0;  // + is left (ccw)
  }

  bool busy() const { return active_; }

 private:
  Pattern pat_{};
  uint8_t idx_ = 0;
  bool phase_on_ = false, active_ = false;
  uint32_t t0_ = 0, steer_t0_ = 0;
  Live live_{};
  bool have_live_ = false, was_off_ = false, was_done_ = false, was_hazard_ = false, tapped_ = false;
  bool was_unmapped_ = false, pat_unmapped_ = false;
  uint32_t unmapped_t0_ = 0;
  char last_act_ = 0, last_dir_ = 0;
};

}  // namespace nudge
