// host test: g++ -std=c++17 -I ../nudge_band test_protocol.cpp -o t && ./t
#include <math.h>
#include <stdio.h>

#include "haptics.h"
#include "protocol.h"

using namespace nudge;
static int fails = 0;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #c); ++fails; } } while (0)

static void parsing() {
  Msg m;
  CHECK(parse("2,T,L,90,4.5", m) && m.kind == STEP);
  CHECK(m.step.i == 2 && m.step.act == 'T' && m.step.dir == 'L' && m.step.deg == 90 && fabsf(m.step.m - 4.5f) < 1e-4);
  CHECK(parse("1,B,R,-48,6.0\r\n", m) && m.step.deg == -48 && m.step.dir == 'R');
  CHECK(parse("0,D,S,0,0.0", m) && m.step.act == 'D');
  CHECK(parse("6,F,U,E,1", m) && m.kind == FLOOR && m.floor.up && m.floor.via == 'E' && m.floor.floor_i == 1);
  CHECK(parse("3,F,D,S,0", m) && !m.floor.up && m.floor.via == 'S');
  CHECK(parse("N,2.4,-15,TR,0", m) && m.kind == LIVE);
  CHECK(m.live.has_m && fabsf(m.live.m_next - 2.4f) < 1e-4 && m.live.has_deg && m.live.deg_next == -15);
  CHECK(m.live.act == 'T' && m.live.dir == 'R' && !m.live.off && !m.live.done);
  CHECK(parse("N,0.9,,AS,2", m) && !m.live.has_deg && m.live.done && !m.live.off);
  CHECK(parse("N,,,-S,1", m) && !m.live.has_m && m.live.off && m.live.act == '-');
  CHECK(parse("C,hazard", m) && m.kind == CUE && !strcmp(m.cue, "hazard"));
  CHECK(parse("N,4.2,170,-S,4", m) && m.live.unmapped && !m.live.hazard && !m.live.off);
  CHECK(parse("N,3.0,0,TL,8", m) && m.live.hazard && !m.live.unmapped);
  CHECK(parse("N,,,-S,5", m) && m.live.unmapped && m.live.off);
  // malformed
  const char* bad[] = {"", "N,2.4,-15,TR", "N,x,1,TR,0", "N,1,1,QR,0", "2,T,L,90", "2,Q,L,90,1.0",
                       "2,T,X,90,1.0", "a,T,L,90,1.0", "6,F,U,Z,1", "6,F,U,E,x", "C,", "1,T,L,9.5,1.0",
                       "0123456789012345678901234567890123"};
  for (const char* b : bad) { bool ok = parse(b, m); CHECK(!ok && m.kind == NONE); if (ok) printf("  accepted: %s\n", b); }
}

static Live live(const char* line) { Msg m; parse(line, m); return m.live; }

static void grammar() {
  CHECK(Engine::steer_period(10) == 0 && Engine::steer_period(-15) == 0);
  CHECK(Engine::steer_period(16) > Engine::steer_period(60) && Engine::steer_period(90) == 300);

  Engine e;
  uint32_t t = 0;
  // on course: silent
  e.on_live(live("N,5.0,3,TL,0"), t);
  for (; t < 2000; t += 10) CHECK(e.tick(t) == 0);
  // need to steer left (+40): left pulses only, repeating
  e.on_live(live("N,5.0,40,TL,0"), t);
  int lefts = 0, rights = 0;
  for (uint32_t end = t + 3000; t < end; t += 5) { uint8_t m = e.tick(t); lefts += (m & M_L) != 0; rights += (m & M_R) != 0; }
  CHECK(lefts > 0 && rights == 0);
  // steer right
  e.on_live(live("N,5.0,-60,TL,0"), t);
  lefts = rights = 0;
  for (uint32_t end = t + 3000; t < end; t += 5) { uint8_t m = e.tick(t); lefts += (m & M_L) != 0; rights += (m & M_R) != 0; }
  CHECK(rights > 0 && lefts == 0);
  // approaching a left turn: the left cue (2 pulses) fires once
  e.on_live(live("N,1.5,0,TL,0"), t);
  CHECK(e.busy());
  int edges = 0; uint8_t prev = 0;
  for (uint32_t end = t + 1000; t < end; t += 5) { uint8_t m = e.tick(t); if (m && !prev) { ++edges; CHECK(m == M_L); } prev = m; }
  CHECK(edges == 2);
  e.on_live(live("N,1.0,0,TL,0"), t);
  CHECK(!e.busy());  // not again for the same maneuver
  // next maneuver: right turn -> 3 pulses on the right
  e.on_live(live("N,1.2,0,TR,0"), t);
  edges = 0; prev = 0;
  for (uint32_t end = t + 1000; t < end; t += 5) { uint8_t m = e.tick(t); if (m && !prev) { ++edges; CHECK(m == M_R); } prev = m; }
  CHECK(edges == 3);
  // off route: one long buzz on both
  e.on_live(live("N,,,-S,1"), t);
  CHECK(e.tick(t + 10) == M_BOTH && e.tick(t + 400) == M_BOTH && e.tick(t + 600) == 0);
  t += 1000;
  // arrived: rising pattern on both, then silence even with a heading error
  e.on_live(live("N,0.5,80,AS,2"), t);
  CHECK(e.busy() && e.tick(t + 10) == M_BOTH);
  for (uint32_t end = t + 2000; t < end; t += 5) e.tick(t);
  CHECK(!e.busy() && e.tick(t) == 0);
  // hazard flag (8) -> rapid buzz
  Engine h;
  h.on_live(live("N,4.0,0,TL,8"), 0);
  edges = 0; prev = 0;
  for (uint32_t s = 0; s < 400; s += 5) { uint8_t m = h.tick(s); if (m && !prev) ++edges; prev = m; }
  CHECK(edges == 5);
  // unmapped flag (4) -> slow long pulses on both, no turn tap, repeated while off the map
  Engine u;
  u.on_live(live("N,1.0,0,TL,4"), 0);
  edges = 0; prev = 0;
  uint8_t seen = 0;
  for (uint32_t s = 0; s < 3000; s += 5) { uint8_t m = u.tick(s); if (m && !prev) ++edges; seen |= m; prev = m; }
  CHECK(edges == 3 && seen == M_BOTH);
  CHECK(u.tick(4100) == M_BOTH);  // reminder after 4 s
  // back on the map: the reminder stops
  u.on_live(live("N,5.0,0,TL,0"), 6000);
  uint8_t any = 0;
  for (uint32_t s = 6000; s < 12000; s += 5) any |= u.tick(s);
  CHECK(any == 0);
}

int main(int argc, char** argv) {
  if (argc > 1 && !strcmp(argv[1], "-")) {  // parse lines from stdin (server output), fail on any reject
    char line[64]; int n = 0, bad = 0; Msg m;
    while (fgets(line, sizeof(line), stdin)) { ++n; if (!parse(line, m)) { ++bad; printf("rejected: %s", line); } }
    printf("%d lines from stdin, %d rejected\n", n, bad);
    return bad ? 1 : 0;
  }
  parsing();
  grammar();
  printf(fails ? "%d failure(s)\n" : "all protocol + haptics checks passed\n", fails);
  return fails ? 1 : 0;
}
