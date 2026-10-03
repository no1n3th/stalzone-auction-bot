import { describe, expect, it } from "vitest";
import { detailToText } from "./api";
describe("detailToText", () => {
  it("passes strings through", () => { expect(detailToText("nope")).toBe("nope"); });
  it("formats 422 arrays", () => {
    expect(detailToText([
      { loc: ["body", "username"], msg: "field required" },
      { loc: ["body", "price"], msg: "not a float" },
    ])).toBe("username: field required; price: not a float");
  });
  it("falls back on unknown shapes", () => {
    expect(detailToText({ x: 1 })).toBe("Ошибка запроса");
    expect(detailToText(null)).toBe("Ошибка запроса");
  });
});
