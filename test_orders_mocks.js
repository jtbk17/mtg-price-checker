// Fake Apps Script services and spreadsheets for test_orders_e2e.py, which
// runs the real google_apps_script.js against them in a browser.
window.__props = { PASSCODE: "pw", SHEET_SHARED: "S", SHEET_JOEL: "J" };
const PropertiesService = { getScriptProperties: () => ({ getProperty: (k) => (k in __props ? __props[k] : null), setProperty: (k, v) => { __props[k] = String(v); } }) };
const LockService = { getScriptLock: () => ({ waitLock() {}, releaseLock() {} }) };
// Off by default so a check that edits the fake sheets directly sees it at
// once; the real script caches the list for a minute.
window.__cacheOn = false;
window.__cache = {};
const CacheService = { getScriptCache: () => ({
  get: (k) => (__cacheOn && k in __cache ? __cache[k] : null),
  put: (k, v) => { __cache[k] = v; },
}) };
const ContentService = { MimeType: { JSON: "json" }, createTextOutput: (s) => ({ s, setMimeType() { return this; } }) };
let __uuid = 0;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const Utilities = {
  getUuid: () => "ord" + String(++__uuid).padStart(5, "0") + "-x",
  // Only the two patterns the script uses, in UTC.
  formatDate: (d, tz, fmt) => fmt === "yyyy-MM-dd" ? d.toISOString().slice(0, 10) : `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`,
};
class Sheet {
  // Like a real tab, a few blank rows below the data until more are inserted.
  constructor(name, rows) { this.name = name; this.rows = rows; this.maxRows = rows.length + 3; }
  // In box is a formula in the real sheet: Qty - Sold - Pulled.
  computed(r, i) {
    if (this.name !== "Inventory" || i === 0) return r.slice();
    const h = this.rows[0], c = (n) => h.indexOf(n), out = r.slice();
    out[c("In box")] = (Number(r[c("Qty")]) || 0) - (Number(r[c("Sold")]) || 0) - (Number(r[c("Pulled")]) || 0);
    return out;
  }
  // Like the real thing: every row padded with "" to the widest one.
  getDataRange() {
    const width = Math.max(...this.rows.map((r) => r.length));
    return { getValues: () => this.rows.map((r, i) => this.computed(r, i).concat(Array(width - r.length).fill(""))) };
  }
  getRange(row, col, numRows = 1, numCols = 1) {
    return {
      setValue: (v) => { this.rows[row - 1][col - 1] = v; },
      setValues: (vals) => {
        if (vals.length !== numRows || vals.some((r) => r.length !== numCols)) throw new Error("setValues: size doesn't match the range");
        if (row + numRows - 1 > this.maxRows) throw new Error("setValues: past the last row of the sheet");
        vals.forEach((r, i) => {
          while (this.rows.length < row + i) this.rows.push([]);
          const target = this.rows[row - 1 + i];
          r.forEach((v, j) => { while (target.length < col - 1 + j) target.push(""); target[col - 1 + j] = v; });
        });
      },
    };
  }
  getMaxRows() { return this.maxRows; }
  insertRowsAfter(after, n) { this.maxRows += n; }
  deleteRows(start, n) { this.rows.splice(start - 1, n); this.maxRows -= n; }
  appendRow(a) { this.rows.push(a.slice()); }
  setFrozenRows() {}
  deleteRow(n) { this.rows.splice(n - 1, 1); this.maxRows -= 1; }
  setName(n) { const b = Object.values(__books).find((bk) => Object.values(bk).includes(this)); for (const k of Object.keys(b)) if (b[k] === this) delete b[k]; b[n] = this; this.name = n; }
}
const H = ["Location", "Name", "Set code", "Set name", "Collector #", "Foil", "Rarity", "Condition", "Language", "Qty", "Price (USD)",
  "Scryfall ID", "Date logged", "Sold", "Pulled", "In box", "Value in box (USD)", "Notes"];
const set = (row, setName) => { row[3] = setName; return row; };
const inv = (loc, name, foil, qty, id) => [loc, name, "X", "Set", "1", foil, "rare", "near_mint", "en", qty, "$1", id, "2026-11-01", "", "", "", "", ""];
window.__books = {
  S: { Inventory: new Sheet("Inventory", [H,
    inv("B01-01", "Psychic Frog", "normal", 1, "frog"),
    inv("B01-02", "Psychic Frog", "normal", 1, "frog"),
    inv("B01-03", "Psychic Frog", "foil", 3, "frog"),
    inv("B04-01", "Sol Ring", "foil", 1, "sol"),
    inv("B04-02", "Wasteland", "normal", 2, "waste"),
    inv("B05-01", "Bulk Card", "normal", 6, "bulk"),
    inv("B05-02", "Bulk Card", "normal", 9, "bulk"),
    inv("B05-03", "Bulk Card", "normal", 3, "bulk")]),
    // Made by the first version of the script: old name, no Type/Source.
    "WTB orders": new Sheet("WTB orders", [
      ["Order", "Status", "Card", "Set", "Finish", "Scryfall ID", "Qty", "Price (USD)", "Location", "Inventory row", "Offered", "Paid", "Closed"],
      ["legacy01", "Awaiting payment", "Old Card", "Set", "normal", "old", 1, 3, "", "", new Date(), "", ""]]) },
  // Joel keeps a copy of each card: two printings of Lightning Bolt, and
  // Secret Lair cards (one Arcane Signet also in a regular printing).
  J: { Inventory: new Sheet("Inventory", [H, inv("B01-01", "Snap", "foil", 1, "snap"), inv("B02-01", "Sol Ring", "foil", 1, "sol"),
    set(inv("B01-02", "Lightning Bolt", "normal", 1, "bolt-a"), "Alpha"),
    set(inv("B01-02", "Lightning Bolt", "foil", 1, "bolt-b"), "Beta"),
    set(inv("B01-03", "Arcane Signet", "normal", 1, "sig-sl"), "Secret Lair Drop"),
    set(inv("B01-03", "Arcane Signet", "normal", 2, "sig"), "Commander Legends"),
    set(inv("B01-03", "Galaxy Sword", "normal", 1, "sword"), "Secret Lair Drop")]),
    Dashboard: new Sheet("Dashboard", [["MTG chaos-sort inventory"], [], ["Settings"], ["Cards per slot", 100], ["Slots per box", 10]]) },
};
const SpreadsheetApp = {
  openById: (id) => {
    const b = __books[id];
    if (!b) throw new Error("no sheet " + id);
    return { getSheetByName: (n) => b[n] || null, insertSheet: (n) => (b[n] = new Sheet(n, [])), getSpreadsheetTimeZone: () => "Asia/Singapore" };
  },
};
