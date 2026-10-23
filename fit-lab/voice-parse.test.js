// Run: node --test fit-lab/voice-parse.test.js
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { parse, parseAlternatives, plausible } = require("./voice-parse.js");

const pick = (r) => {
  const o = { type: r.type };
  for (const k of ["no", "value", "variant"]) if (r[k] !== undefined) o[k] = r[k];
  return o;
};
const cases = [
  // item number + value
  ["1番8.45", { type: "set", no: 1, value: 8.45 }],
  ["1番 8.45", { type: "set", no: 1, value: 8.45 }],
  ["3番 2.3", { type: "set", no: 3, value: 2.3 }],
  ["いちばん はってんよんご", { type: "set", no: 1, value: 8.45 }],
  ["イチバン ハッテンヨンゴ", { type: "set", no: 1, value: 8.45 }],
  ["一番八点四五", { type: "set", no: 1, value: 8.45 }],
  ["１番　８．４５", { type: "set", no: 1, value: 8.45 }],
  ["じゅうにばん にじゅうろくてんななはち", { type: "set", no: 12, value: 26.78 }],
  ["12番目 26.70", { type: "set", no: 12, value: 26.7 }],
  ["5番は8.2", { type: "set", no: 5, value: 8.2 }],
  ["2番に4.21", { type: "set", no: 2, value: 4.21 }],
  ["3番、8点2ミリ", { type: "set", no: 3, value: 8.2 }],
  ["さんばん にてんさん", { type: "set", no: 3, value: 2.3 }],
  // value alone → cursor row
  ["8.45", { type: "set", value: 8.45 }],
  ["はちてんよんご", { type: "set", value: 8.45 }],
  ["れいてんいちに", { type: "set", value: 0.12 }],
  ["ゼロ点ニ", { type: "set", value: 0.2 }],
  ["てんよんご", { type: "set", value: 0.45 }],
  ["えーと 8.3", { type: "set", value: 8.3 }],
  ["8.45mm", { type: "set", value: 8.45 }],
  ["はってん", { type: "set", value: 8 }],
  ["2番 はち", { type: "set", no: 2, value: 8 }],
  ["きゅうてんきゅう", { type: "set", value: 9.9 }],
  ["ろくてんごー", { type: "set", value: 6.5 }],
  ["しちてんなな", { type: "set", value: 7.7 }],
  // cursor moves
  ["5番", { type: "cursor", no: 5 }],
  ["ごばん", { type: "cursor", no: 5 }],
  ["次", { type: "next" }],
  ["つぎ", { type: "next" }],
  ["前", { type: "prev" }],
  ["まえ", { type: "prev" }],
  // variants
  ["ブイ1", { type: "variant", variant: 1 }],
  ["V2", { type: "variant", variant: 2 }],
  ["ｖ３", { type: "variant", variant: 3 }],
  ["バリアント2", { type: "variant", variant: 2 }],
  ["ブイいち", { type: "variant", variant: 1 }],
  ["共通", { type: "variant", variant: "*" }],
  // commands
  ["取り消し", { type: "undo" }],
  ["とりけし", { type: "undo" }],
  ["戻して", { type: "undo" }],
  ["保存", { type: "save" }],
  ["保存して", { type: "save" }],
  ["はい", { type: "confirm" }],
  ["OK", { type: "confirm" }],
  ["オッケー", { type: "confirm" }],
  // rejected
  ["こんにちは", { type: "unknown" }],
  ["1番 8.4.5", { type: "unknown" }],
  ["", { type: "unknown" }],
  ["ブイ", { type: "unknown" }],
];

for (const [text, want] of cases) {
  test(`parse ${JSON.stringify(text)}`, () => assert.deepEqual(pick(parse(text)), want));
}

test("parse keeps the normalised text", () => {
  assert.equal(parse("１番　８．４５").norm, "1番8.45");
});

test("parseAlternatives picks the first known alternative", () => {
  const r = parseAlternatives(["こんにちは", "1番8.45", "8.45"]);
  assert.equal(r.type, "set");
  assert.equal(r.no, 1);
  assert.equal(r.raw, "1番8.45");
  assert.equal(parseAlternatives(["あいう"]).type, "unknown");
});

test("plausible: within max(1.0 mm, 15 % of CAD)", () => {
  assert.equal(plausible(8.45, 8.3), true);
  assert.equal(plausible(9.29, 8.3), true);     // Δ 0.99
  assert.equal(plausible(84.5, 8.3), false);    // missing decimal point
  assert.equal(plausible(845, 8.3), false);
  assert.equal(plausible(26.7, 26.78), true);
  assert.equal(plausible(30.5, 26.78), true);   // Δ 3.72 < 15 % (4.02)
  assert.equal(plausible(31.0, 26.78), false);  // Δ 4.22 > 4.02
  assert.equal(plausible(0.12, 0.2), true);     // small CAD: 1.0 mm floor
  assert.equal(plausible(1.5, 0.2), false);
  assert.equal(plausible(8.3, null), false);    // no CAD → always confirm
});
