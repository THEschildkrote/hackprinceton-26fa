// parser for the nudge device lines (contract: nudge/device.py docstring). pure c++, no arduino deps.
//   route step: "<i>,<D|T|B|U|S|W|A>,<L|R|S>,<deg>,<m>"     e.g. "2,T,L,90,4.5"
//   floor step: "<i>,F,<U|D>,<E|S|X>,<floor#>"              e.g. "6,F,U,E,1"
//   live:       "N,<m_next>,<deg_next>,<act><dir>,<flags>"  e.g. "N,2.4,-15,TR,0"   (m/deg may be empty)
//   cue (band extension, not from the server): "C,<cue>"    e.g. "C,hazard"
#pragma once
#include <stdlib.h>
#include <string.h>

namespace nudge {

enum Kind { NONE = 0, STEP, FLOOR, LIVE, CUE };

struct Step { int i; char act; char dir; int deg; float m; };
struct Floor { int i; bool up; char via; int floor_i; };
struct Live {
  bool has_m; float m_next;
  bool has_deg; int deg_next;
  char act; char dir;
  bool off; bool done; bool unmapped; bool hazard;  // flags 1 off route, 2 arrived, 4 unmapped, 8 hazard ahead
};
struct Msg {
  Kind kind;
  Step step; Floor floor; Live live;
  char cue[12];
};

namespace detail {
// split on commas keeping empty fields; returns count. buf is modified.
inline int split(char* buf, char** f, int maxf) {
  int n = 0;
  f[n++] = buf;
  for (char* p = buf; *p && n < maxf; ++p) {
    if (*p == ',') { *p = 0; f[n++] = p + 1; }
  }
  return n;
}
inline bool is_int(const char* s) {
  if (*s == '-') ++s;
  if (!*s) return false;
  for (; *s; ++s) if (*s < '0' || *s > '9') return false;
  return true;
}
inline bool is_num(const char* s) {
  if (*s == '-') ++s;
  bool digit = false, dot = false;
  for (; *s; ++s) {
    if (*s >= '0' && *s <= '9') digit = true;
    else if (*s == '.' && !dot) dot = true;
    else return false;
  }
  return digit;
}
inline bool one_of(char c, const char* set) { return c && strchr(set, c) != 0; }
}  // namespace detail

// parse one line (with or without trailing \r\n). returns false and kind NONE on anything malformed.
inline bool parse(const char* line, Msg& out) {
  using namespace detail;
  memset(&out, 0, sizeof(out));
  char buf[32];
  size_t len = strlen(line);
  while (len && (line[len - 1] == '\n' || line[len - 1] == '\r')) --len;
  if (len == 0 || len >= sizeof(buf)) return false;
  memcpy(buf, line, len);
  buf[len] = 0;
  char* f[7];
  int n = split(buf, f, 7);

  if (strcmp(f[0], "N") == 0) {
    if (n != 5 || strlen(f[3]) != 2 || !is_int(f[4])) return false;
    Live& l = out.live;
    if (*f[1]) { if (!is_num(f[1])) return false; l.has_m = true; l.m_next = (float)atof(f[1]); }
    if (*f[2]) { if (!is_int(f[2])) return false; l.has_deg = true; l.deg_next = atoi(f[2]); }
    if (!one_of(f[3][0], "DTBUSWAF-") || !one_of(f[3][1], "LRS")) return false;
    l.act = f[3][0]; l.dir = f[3][1];
    int fl = atoi(f[4]);
    l.off = fl & 1; l.done = (fl & 2) != 0; l.unmapped = (fl & 4) != 0; l.hazard = (fl & 8) != 0;
    out.kind = LIVE;
    return true;
  }
  if (strcmp(f[0], "C") == 0) {
    if (n != 2 || !*f[1] || strlen(f[1]) >= sizeof(out.cue)) return false;
    strcpy(out.cue, f[1]);
    out.kind = CUE;
    return true;
  }
  if (!is_int(f[0]) || n != 5 || strlen(f[1]) != 1) return false;
  int i = atoi(f[0]);
  if (f[1][0] == 'F') {
    if (strlen(f[2]) != 1 || !one_of(f[2][0], "UD") || strlen(f[3]) != 1 || !one_of(f[3][0], "ESX") || !is_int(f[4]))
      return false;
    out.floor = Floor{i, f[2][0] == 'U', f[3][0], atoi(f[4])};
    out.kind = FLOOR;
    return true;
  }
  if (!one_of(f[1][0], "DTBUSWA") || strlen(f[2]) != 1 || !one_of(f[2][0], "LRS") || !is_int(f[3]) || !is_num(f[4]))
    return false;
  out.step = Step{i, f[1][0], f[2][0], atoi(f[3]), (float)atof(f[4])};
  out.kind = STEP;
  return true;
}

}  // namespace nudge
