import { buildingLabel, placeNames, type Place } from "../api";

describe("placeNames", () => {
  const p = (name: string, kind = "room", extra: Partial<Place> = {}): Place => ({ id: name, name, kind, ...extra });

  it("puts useful rooms first, hides Other and unnamed, corridors last", () => {
    const names = placeNames([
      p("Hallway 1, by the kitchen"), p("Other"), p("Closet"), p("Kitchen"), p("Bathroom, Floor 1"),
      p("unnamed space 1", "room", { unnamed: true }), p("water fountain", "feature"), p("Living Room, Floor 1"),
    ]);
    expect(names.slice(0, 3)).toEqual(["Bathroom, Floor 1", "Kitchen", "Living Room, Floor 1"]);
    expect(names).not.toContain("Other");
    expect(names).not.toContain("unnamed space 1");
    expect(names.indexOf("Closet")).toBeGreaterThan(names.indexOf("Living Room, Floor 1"));
    expect(names[names.length - 1]).toBe("water fountain");  // features after rooms
  });

  it("labels buildings by address", () => {
    expect(buildingLabel({ sid: "x", address: "1255 S Atlantic Blvd", city: "Los Angeles" })).toBe("1255 S Atlantic Blvd, Los Angeles");
  });
});
