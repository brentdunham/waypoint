import { describe, expect, it } from "vitest";

import { keyFromHash } from "./client";

const KEY = "ak_1a2b3c4d5e6f.Abc-def_123";

describe("keyFromHash", () => {
  it("takes the key and leaves the rest of the fragment", () => {
    expect(keyFromHash(`#key=${KEY}`)).toEqual({ key: KEY, rest: "" });
    expect(keyFromHash(`#key=${KEY}&tab=research`)).toEqual({ key: KEY, rest: "tab=research" });
  });

  it("ignores fragments without a well-formed key", () => {
    expect(keyFromHash("")).toBeNull();
    expect(keyFromHash("#section-2")).toBeNull();
    expect(keyFromHash("#key=")).toBeNull();
    expect(keyFromHash("#key=not-a-key")).toBeNull();
    expect(keyFromHash("#key=ak_abc.<script>")).toBeNull();
  });
});
