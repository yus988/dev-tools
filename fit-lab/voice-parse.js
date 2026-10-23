/* fit-lab voice command parser (Japanese). Pure functions; works in the browser (window.FitVoice) and in Node (require). */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.FitVoice = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  /** Full-width ASCII → half-width, katakana → hiragana, lower case, drop spaces and punctuation. */
  function basicNormalize(text) {
    return String(text || "")
      .replace(/[！-～]/g, (c) => String.fromCharCode(c.charCodeAt(0) - 0xfee0))
      .replace(/[ァ-ヶ]/g, (c) => String.fromCharCode(c.charCodeAt(0) - 0x60))
      .replace(/　/g, " ")
      .toLowerCase()
      .replace(/[\s、。,，!！?？「」『』・:：]/g, "");
  }

  // numeral words → token characters (0-9, X = 十, H = 百, "." = 点). Longest match first.
  const NUM_WORDS = [
    ["ぜろ", "0"], ["れい", "0"], ["まる", "0"], ["零", "0"], ["〇", "0"],
    ["いち", "1"], ["いっ", "1"], ["一", "1"],
    ["にー", "2"], ["にい", "2"], ["に", "2"], ["二", "2"],
    ["さん", "3"], ["三", "3"],
    ["よん", "4"], ["よー", "4"], ["四", "4"],
    ["ごー", "5"], ["ごう", "5"], ["ご", "5"], ["五", "5"],
    ["ろく", "6"], ["ろっ", "6"], ["六", "6"],
    ["なな", "7"], ["しち", "7"], ["七", "7"],
    ["はち", "8"], ["はっ", "8"], ["八", "8"],
    ["きゅう", "9"], ["きゅー", "9"], ["きゅ", "9"], ["九", "9"],
    ["じゅう", "X"], ["じゅっ", "X"], ["じっ", "X"], ["じゅ", "X"], ["十", "X"],
    ["ひゃく", "H"], ["びゃく", "H"], ["ぴゃく", "H"], ["百", "H"],
    ["てん", "."], ["点", "."], ["ぽいんと", "."],
  ];
  const NUM_RE = new RegExp(NUM_WORDS.map((w) => w[0]).sort((a, b) => b.length - a.length).join("|"), "g");
  const NUM_MAP = Object.fromEntries(NUM_WORDS);

  /** Integer from tokens like "12", "X2" (12), "2X" (20), "1H5" (105). NaN if malformed. */
  function evalInt(s) {
    if (s === "") return NaN;
    if (!/[XH]/.test(s)) return /^\d+$/.test(s) ? parseInt(s, 10) : NaN;
    const m = s.match(/^(?:(\d)?H)?(?:(\d)?X)?(\d)?$/);
    if (!m) return NaN;
    let v = 0;
    if (s.includes("H")) v += (m[1] ? +m[1] : 1) * 100;
    if (s.includes("X")) v += (m[2] ? +m[2] : 1) * 10;
    if (m[3]) v += +m[3];
    return v;
  }

  /** Replace numeral words with digits and evaluate 十/百 groups: "はってんよんご" → "8.45", "じゅうにばん" → "12ばん". */
  function numeralize(s) {
    const tok = s.replace(NUM_RE, (w) => NUM_MAP[w]);
    return tok.replace(/[0-9XH]+/g, (run) => {
      if (!/[XH]/.test(run)) return run;
      const v = evalInt(run);
      return Number.isNaN(v) ? run : String(v);
    });
  }

  const FILLER_RE = /(えーと|えっと|えー|あー|うーん|みり|mm|ー|[はのがをでねっ])/g;
  const VALUE_RE = /^(\d+(?:\.\d*)?|\.\d+)$/;   // "8." (はってん) = 8

  function toValue(s) {
    if (!VALUE_RE.test(s)) return null;
    const v = parseFloat(s.startsWith(".") ? "0" + s : s);
    return Number.isFinite(v) ? v : null;
  }

  /**
   * Parse one final transcript.
   * Returns {type, ...} with type one of:
   *   set {no?, value}   value for item `no`, or for the cursor row when `no` is absent
   *   cursor {no}        move the cursor to item `no`
   *   next | prev | undo | save | confirm
   *   variant {variant}  1-based variant number, or "*" for 共通
   *   unknown
   * Every result carries `norm`, the normalised text, for logging.
   */
  function parse(text) {
    const pre = basicNormalize(text);
    const out = (o) => Object.assign(o, { norm: pre });
    if (!pre) return out({ type: "unknown" });

    if (/^(はい|ok|okay|おっけー|おけー|おーけー|了解|りょうかい|確定|かくてい)$/.test(pre)) return out({ type: "confirm" });
    if (/(取り?消|とりけし|もどして|戻して|きゃんせる|やりなおし|やり直し)/.test(pre)) return out({ type: "undo" });
    if (/(保存|ほぞん|せーぶ)/.test(pre)) return out({ type: "save" });
    if (/^(次|つぎ|ねくすと)(へ|に)?$/.test(pre)) return out({ type: "next" });
    if (/^(前|まえ|もどる|戻る)(へ|に)?$/.test(pre)) return out({ type: "prev" });
    if (/^(共通|きょうつう|ぜんぶ|全部)$/.test(pre)) return out({ type: "variant", variant: "*" });
    const vm = pre.match(/^(?:v|ぶい|ゔい|ばりあんと|へんしゅ)(.+)$/);
    if (vm) {
      const n = numeralize(vm[1]).replace(FILLER_RE, "");
      if (/^\d+$/.test(n)) return out({ type: "variant", variant: parseInt(n, 10) });
      return out({ type: "unknown" });
    }

    let s = pre
      .replace(/ばんめ|番目|ばん/g, "番")
      .replace(/番(に|は|の|が)(?=[0-9])/g, "番");   // particle only before written digits: "2番に4.21", but "3番にてんさん" = 2.3
    s = numeralize(s).replace(FILLER_RE, "");
    if (/[^0-9.番]/.test(s)) return out({ type: "unknown" });

    const m = s.match(/^(\d+)番(.*)$/);
    if (m) {
      const no = parseInt(m[1], 10);
      if (m[2] === "") return out({ type: "cursor", no });
      const value = toValue(m[2]);
      return value == null ? out({ type: "unknown" }) : out({ type: "set", no, value });
    }
    const value = toValue(s);
    return value == null ? out({ type: "unknown" }) : out({ type: "set", value });
  }

  /** A value is plausible when CAD is known and |value − CAD| ≤ max(1.0, 15 % of CAD). */
  function plausible(value, cad) {
    if (typeof cad !== "number" || !Number.isFinite(cad) || !Number.isFinite(value)) return false;
    return Math.abs(value - cad) <= Math.max(1.0, Math.abs(cad) * 0.15);
  }

  /** Pick the first alternative transcript that parses to something known. */
  function parseAlternatives(texts) {
    let first = null;
    for (const t of texts) {
      const r = parse(t);
      r.raw = t;
      if (!first) first = r;
      if (r.type !== "unknown") return r;
    }
    return first || { type: "unknown", raw: "", norm: "" };
  }

  return { parse, parseAlternatives, plausible, numeralize, basicNormalize };
});
