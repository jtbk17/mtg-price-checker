// Google Apps Script web app: lets the GitHub Pages dashboard
// (docs/index.html) record sales — to WTB buyers or from our TCGMP
// listings — and their progress in the two inventory sheets.
// The dashboard is a static page with nowhere safe to keep Google
// credentials, so this runs inside Google as you, and the dashboard only
// ever sends it a passcode.
//
// Deploy (one-time):
//   1. script.google.com -> New project, and paste this file over Code.gs.
//   2. Project Settings -> Script properties, add:
//        PASSCODE      a long random string; the dashboard asks for it once
//                      per device
//        SHEET_SHARED  the shared inventory spreadsheet's ID
//        SHEET_JOEL    Joel's inventory spreadsheet's ID
//   3. Deploy -> New deployment -> type Web app. Execute as: Me. Who has
//      access: Anyone. Authorize when asked (it edits both sheets as you,
//      so your account needs edit access to both).
//   4. Put the web app URL in SHEETS_API_URL in docs/index.html.
// After changing this file: Deploy -> Manage deployments -> edit -> Version:
// New version, which keeps the same URL.
//
// Each spreadsheet gets an "Orders" tab (created on first use; an older
// "WTB orders" tab is renamed and given any newer columns) logging every
// order and its progress. "Not a sale" answers to the dashboard's "Looks
// sold?" listing drops are kept in script properties, not the sheets.
// Inventory's Sold/Pulled cells only change at these steps:
//   buyer paid         Pulled + qty on the row(s) the copies came from (you
//                      pick each row and how many, when they're spread out)
//   dropped off        Pulled - qty, Sold + qty — In box is Qty - Sold -
//                      Pulled, so adding to Sold alone would count the
//                      copies out twice
//   cancelled after    Pulled - qty (the copies go back in the box)
//   being paid

const INVENTORY_TAB = "Inventory";
const ORDERS_TAB = "Orders";
const LEGACY_ORDERS_TAB = "WTB orders";
// New columns only ever go on the end, so older tabs can be extended in place.
const ORDER_HEADERS = [
  "Order", "Status", "Card", "Set", "Finish", "Scryfall ID", "Qty", "Price (USD)",
  "Location", "Inventory row", "Offered", "Paid", "Closed", "Type", "Source",
];
const ORDER_TYPES = ["WTB", "Listing"];
const SHEET_PROPERTIES = { Shared: "SHEET_SHARED", Joel: "SHEET_JOEL" };
const RECENT_DAYS = 14;

// Bumped when the dashboard needs to know what this deployment supports
// (3: an order's copies can be pulled from several Inventory rows; 4: the
// list carries each waiting order's box locations, every change answers
// with the fresh list, and cancelled orders can be removed; 5: undo, several
// orders dropped off at once, a sales summary, and a cached list).
const VERSION = 5;
// A mis-tap can be stepped back for this long.
const UNDO_HOURS = 24;
// The list is cached this long between changes (each change refreshes it),
// so a hand edit in the sheet can take up to a minute to show.
const LIST_CACHE_SECONDS = 60;

const AWAITING = "Awaiting payment";
const PAID = "Paid";
const SOLD = "Sold";
const CANCELLED = "Cancelled";

function doPost(e) {
  let result;
  try {
    const req = JSON.parse(e.postData.contents);
    const passcode = PropertiesService.getScriptProperties().getProperty("PASSCODE");
    if (!passcode || req.passcode !== passcode) {
      result = { ok: false, auth: true, error: "Wrong passcode" };
    } else if (!ACTIONS.hasOwnProperty(req.action)) {
      result = { ok: false, error: `Unknown action "${req.action}"` };
    } else if (req.action === "locations") {
      // Only reads, and the dashboard asks once per open order, so it
      // mustn't queue behind (or hold up) the lock real changes need.
      result = Object.assign({ ok: true }, ACTIONS.locations(req));
    } else {
      // One request at a time: two taps landing together must not both
      // read the same Pulled value and each add to it.
      const lock = LockService.getScriptLock();
      lock.waitLock(20000);
      try {
        result = Object.assign({ ok: true }, ACTIONS[req.action](req));
        // Answer every change with the fresh list, saving the dashboard a
        // second round trip (each one costs seconds of start-up).
        if (req.action !== "list") Object.assign(result, freshList());
      } finally {
        lock.releaseLock();
      }
    }
  } catch (err) {
    result = { ok: false, error: String((err && err.message) || err) };
  }
  return ContentService.createTextOutput(JSON.stringify(result)).setMimeType(ContentService.MimeType.JSON);
}

const ACTIONS = {
  list() {
    const hit = CacheService.getScriptCache().get("list");
    return hit ? JSON.parse(hit) : freshList();
  },

  // Steps an order back one stage, for a mis-tap within UNDO_HOURS: Paid ->
  // Awaiting payment (copies back in the box), Sold -> Paid (back from Sold
  // to Pulled), Cancelled -> where it was before.
  undo(req) {
    const { order, update } = findOrder(req.owner, req.id);
    const when = order.status === PAID ? order.paid : order.closed;
    if (!when || Date.now() - new Date(when).getTime() > UNDO_HOURS * 60 * 60 * 1000) {
      throw new Error(`Only changes from the last ${UNDO_HOURS} hours can be undone`);
    }
    const inv = readInventory(req.owner);
    if (order.status === PAID) {
      pulledRows(inv, order).forEach(({ row, qty }) => inv.add(row, "Pulled", -qty));
      update({ "Status": AWAITING, "Location": "", "Inventory row": "", "Paid": "" });
    } else if (order.status === SOLD) {
      pulledRows(inv, order, "sold").forEach(({ row, qty }) => {
        inv.add(row, "Sold", -qty);
        inv.add(row, "Pulled", qty);
      });
      update({ "Status": PAID, "Closed": "" });
    } else if (order.status === CANCELLED && order.pulls.length) {
      // Cancelled after being paid: its copies went back in the box.
      pulledRows(inv, order, "inBox").forEach(({ row, qty }) => inv.add(row, "Pulled", qty));
      update({ "Status": PAID, "Closed": "" });
    } else if (order.status === CANCELLED) {
      update({ "Status": AWAITING, "Closed": "" });
    } else {
      throw new Error(`Nothing to undo on an order that's ${order.status}`);
    }
    return {};
  },

  // Several paid orders dropped off together. Every one is checked before
  // any changes, including two orders that pulled from the same row.
  droppedOffMany(req) {
    const found = (req.orders || []).map((o) => Object.assign({ owner: o.owner }, findOrder(o.owner, o.id)));
    if (!found.length) throw new Error("No orders picked");
    const invs = {};
    const need = new Map();
    const plans = found.map(({ owner, order }) => {
      if (order.status !== PAID) throw new Error(`${order.name} is ${order.status}, not ${PAID}`);
      const inv = invs[owner] || (invs[owner] = readInventory(owner));
      const rows = pulledRows(inv, order);
      rows.forEach(({ row, qty }) => need.set(row, (need.get(row) || 0) + qty));
      return { inv, rows };
    });
    for (const [row, qty] of need) {
      if (row.pulled < qty) throw new Error(`Pulled is only ${row.pulled} at ${row.location} — fix Sold/Pulled by hand`);
    }
    plans.forEach(({ inv, rows }) => rows.forEach(({ row, qty }) => {
      inv.add(row, "Pulled", -qty);
      inv.add(row, "Sold", qty);
    }));
    const closed = new Date();
    found.forEach(({ update }) => update({ "Status": SOLD, "Closed": closed }));
    return {};
  },

  // Deletes a cancelled order's row from the Orders tab. One that came
  // from a "Looks sold?" listing drop leaves that drop dismissed, so it
  // doesn't come back asking.
  remove(req) {
    const { order, sheet } = findOrder(req.owner, req.id);
    if (order.status !== CANCELLED) throw new Error(`Only cancelled orders can be deleted (this one is ${order.status})`);
    if (order.source) {
      const dismissed = readDismissed();
      dismissed[order.source] = new Date().toISOString();
      PropertiesService.getScriptProperties().setProperty("DISMISSED", JSON.stringify(dismissed));
    }
    sheet.deleteRow(order.sheetRow);
    return {};
  },

  // "Not a sale" for a listing drop the dashboard flagged (its id).
  dismiss(req) {
    if (!req.source) throw new Error("Missing which listing drop to dismiss");
    const dismissed = readDismissed();
    dismissed[req.source] = new Date().toISOString();
    PropertiesService.getScriptProperties().setProperty("DISMISSED", JSON.stringify(dismissed));
    return {};
  },

  // Inventory rows holding this printing with copies still in the box.
  locations(req) {
    return { locations: locationsOf(readInventory(req.owner), req.scryfallId, req.finish) };
  },

  offer(req) {
    const qty = Number(req.qty);
    if (!Number.isInteger(qty) || qty < 1) throw new Error("Quantity must be a whole number of at least 1");
    if (!req.scryfallId || !req.finish) throw new Error("Missing card details");
    const type = req.type || "WTB";
    if (ORDER_TYPES.indexOf(type) < 0) throw new Error(`Unknown order type "${type}"`);
    const sheet = ordersSheet(req.owner);
    // One order per flagged listing drop, even if "record" is tapped twice —
    // checked across both sheets, since either owner's copy can be picked.
    const owners = Object.keys(SHEET_PROPERTIES).filter(sheetIdFor);
    if (req.source && owners.some((o) => readOrders(o).orders.some((order) => order.source === req.source))) {
      throw new Error("That sale is already recorded");
    }
    const id = Utilities.getUuid().slice(0, 8);
    const values = {
      "Order": id, "Status": AWAITING, "Card": req.name || "", "Set": req.set || "", "Finish": req.finish,
      "Scryfall ID": req.scryfallId, "Qty": qty, "Price (USD)": Number(req.price) || "", "Location": "",
      "Inventory row": "", "Offered": new Date(), "Paid": "", "Closed": "", "Type": type, "Source": req.source || "",
    };
    sheet.appendRow(ORDER_HEADERS.map((h) => values[h]));
    return { order: findOrder(req.owner, id).order };
  },

  // req.picks: [{row, qty}] adding up to the order's quantity, since the
  // copies can come from several box locations. (Older dashboards send
  // req.row alone, meaning every copy from that row.)
  paid(req) {
    const { order, update } = findOrder(req.owner, req.id);
    if (order.status !== AWAITING) throw new Error(`Order is ${order.status}, not ${AWAITING}`);
    const picks = (req.picks || [{ row: req.row, qty: order.qty }]).map((p) => ({ row: Number(p.row), qty: Number(p.qty) }));
    if (picks.some((p) => !Number.isInteger(p.qty) || p.qty < 1)) throw new Error("Each location needs a whole number of copies");
    if (new Set(picks.map((p) => p.row)).size !== picks.length) throw new Error("The same row was picked twice");
    const total = picks.reduce((n, p) => n + p.qty, 0);
    if (total !== order.qty) throw new Error(`Picked ${total} of the ${order.qty} copies`);

    // Check every row before changing any, so a bad pick changes nothing.
    const inv = readInventory(req.owner);
    const rows = picks.map((p) => {
      const row = inv.rows.find((r) => r.row === p.row);
      if (!row || row.scryfallId !== order.scryfallId || row.finish !== order.finish) {
        throw new Error("An Inventory row no longer holds this card — reload and pick again");
      }
      if (row.inBox < p.qty) throw new Error(`Only ${row.inBox} in the box at ${row.location}`);
      return row;
    });
    picks.forEach((p, i) => inv.add(rows[i], "Pulled", p.qty));
    update(Object.assign({ "Status": PAID, "Paid": new Date() }, pullsColumns(picks.map((p, i) => ({ row: p.row, location: rows[i].location, qty: p.qty })))));
    return { order: findOrder(req.owner, req.id).order };
  },

  droppedOff(req) {
    const { order, update } = findOrder(req.owner, req.id);
    if (order.status !== PAID) throw new Error(`Order is ${order.status}, not ${PAID}`);
    const inv = readInventory(req.owner);
    pulledRows(inv, order).forEach(({ row, qty }) => {
      inv.add(row, "Pulled", -qty);
      inv.add(row, "Sold", qty);
    });
    update({ "Status": SOLD, "Closed": new Date() });
    return { order: findOrder(req.owner, req.id).order };
  },

  cancel(req) {
    const { order, update } = findOrder(req.owner, req.id);
    if (order.status === PAID) {
      const inv = readInventory(req.owner);
      pulledRows(inv, order).forEach(({ row, qty }) => inv.add(row, "Pulled", -qty));
    } else if (order.status !== AWAITING) {
      throw new Error(`Order is already ${order.status}`);
    }
    update({ "Status": CANCELLED, "Closed": new Date() });
    return { order: findOrder(req.owner, req.id).order };
  },
};

// Recent and open orders (waiting ones with where their copies are), the
// sales summary, and what this deployment supports — cached until the
// next change or LIST_CACHE_SECONDS.
function freshList() {
  const orders = [];
  const everySold = [];
  const cutoff = Date.now() - RECENT_DAYS * 24 * 60 * 60 * 1000;
  for (const owner of Object.keys(SHEET_PROPERTIES)) {
    if (!sheetIdFor(owner)) continue;
    const all = readOrders(owner).orders;
    everySold.push(...all.filter((o) => o.status === SOLD || o.status === PAID));
    const mine = all.filter((order) => {
      const open = order.status === AWAITING || order.status === PAID;
      return open || (order.closed && new Date(order.closed).getTime() >= cutoff);
    });
    // Where each waiting order's copies are, read once per sheet here
    // rather than in a call per order.
    const waiting = mine.filter((o) => o.status === AWAITING);
    if (waiting.length) {
      const inv = readInventory(owner);
      waiting.forEach((o) => (o.locations = locationsOf(inv, o.scryfallId, o.finish)));
    }
    orders.push(...mine);
  }
  const list = { orders, dismissed: Object.keys(readDismissed()), version: VERSION, sales: salesSummary(everySold) };
  try {
    CacheService.getScriptCache().put("list", JSON.stringify(list), LIST_CACHE_SECONDS);
  } catch (err) {
    // Over the cache's 100KB limit: just serve it uncached.
  }
  return list;
}

// Sales by when they were dropped off (gross, before TCGMP's commission,
// which the dashboard takes off), plus what paid orders will bring in.
function salesSummary(orders) {
  const now = new Date();
  const monthStart = new Date(now.getFullYear(), now.getMonth(), 1).getTime();
  const lastMonthStart = new Date(now.getFullYear(), now.getMonth() - 1, 1).getTime();
  const weekAgo = now.getTime() - 7 * 24 * 60 * 60 * 1000;
  const periods = {
    week: (t) => t >= weekAgo,
    month: (t) => t >= monthStart,
    lastMonth: (t) => t >= lastMonthStart && t < monthStart,
    all: () => true,
  };
  const blank = () => ({ orders: 0, copies: 0, gross: 0, WTB: 0, Listing: 0 });
  const out = { pending: blank() };
  Object.keys(periods).forEach((name) => (out[name] = blank()));
  const add = (bucket, o) => {
    const gross = (o.price || 0) * o.qty;
    bucket.orders += 1;
    bucket.copies += o.qty;
    bucket.gross += gross;
    bucket[o.type === "Listing" ? "Listing" : "WTB"] += gross;
  };
  for (const o of orders) {
    if (o.status === PAID) {
      add(out.pending, o);
    } else if (o.closed) {
      const t = new Date(o.closed).getTime();
      Object.entries(periods).forEach(([name, inPeriod]) => inPeriod(t) && add(out[name], o));
    }
  }
  for (const bucket of Object.values(out)) {
    ["gross", "WTB", "Listing"].forEach((k) => (bucket[k] = Math.round(bucket[k] * 100) / 100));
  }
  return out;
}

function locationsOf(inv, scryfallId, finish) {
  return inv.rows
    .filter((r) => r.scryfallId === scryfallId && r.finish === finish && r.inBox > 0)
    .map((r) => ({ row: r.row, location: r.location, condition: r.condition, inBox: r.inBox }));
}

// How a paid order records where its copies came from. One row keeps the
// plain form ("B01-02" / 3); several are listed with their counts
// ("B01-01 ×3, B01-02 ×7" / "2 ×3, 3 ×7") — "×" so Sheets never reads them
// as a number or a time.
function pullsColumns(pulls) {
  if (pulls.length === 1) return { "Location": pulls[0].location, "Inventory row": pulls[0].row };
  return {
    "Location": pulls.map((p) => `${p.location} ×${p.qty}`).join(", "),
    "Inventory row": pulls.map((p) => `${p.row} ×${p.qty}`).join(", "),
  };
}

function parsePulls(location, inventoryRow, qty) {
  const rows = String(inventoryRow).match(/\d+\s*×\s*\d+/g);
  if (!rows) {
    const row = Number(inventoryRow);
    return row ? [{ row, location: String(location), qty }] : [];
  }
  const locations = String(location).split(",").map((l) => l.replace(/×\s*\d+\s*$/, "").trim());
  return rows.map((r, i) => {
    const [row, n] = r.split("×").map((x) => Number(x.trim()));
    return { row, location: locations[i] || "", qty: n };
  });
}

// The rows a paid order pulled from, each checked against what it recorded
// — rows are never deleted, but a sort of the Inventory tab would move them
// — and that each still has the copies about to be moved out of `from`
// (pulled, sold, or inBox when putting cancelled copies back).
const FROM_LABELS = { pulled: "Pulled", sold: "Sold", inBox: "In box" };
function pulledRows(inv, order, from = "pulled") {
  if (!order.pulls.length) throw new Error("This order doesn't say which rows it pulled from — fix Sold/Pulled by hand");
  return order.pulls.map((p) => {
    const row = inv.rows.find((r) => r.row === p.row);
    if (!row || row.scryfallId !== order.scryfallId || row.finish !== order.finish || row.location !== p.location) {
      throw new Error(`Inventory row ${p.row} no longer holds this card at ${p.location} — fix Sold/Pulled by hand`);
    }
    if (row[from] < p.qty) {
      throw new Error(`${FROM_LABELS[from]} is only ${row[from]} at ${row.location} — fix Sold/Pulled by hand`);
    }
    return { row, qty: p.qty };
  });
}

function sheetIdFor(owner) {
  const key = SHEET_PROPERTIES[owner];
  return key ? PropertiesService.getScriptProperties().getProperty(key) : null;
}

function spreadsheet(owner) {
  const id = sheetIdFor(owner);
  if (!id) throw new Error(`No sheet set up for ${owner}`);
  return SpreadsheetApp.openById(id);
}

function columnsOf(headers, names, tab) {
  const cols = {};
  for (const name of names) {
    const i = headers.indexOf(name);
    if (i < 0) throw new Error(`${tab} tab has no "${name}" column`);
    cols[name] = i;
  }
  return cols;
}

function readInventory(owner) {
  const sheet = spreadsheet(owner).getSheetByName(INVENTORY_TAB);
  if (!sheet) throw new Error(`${owner}'s sheet has no ${INVENTORY_TAB} tab`);
  const values = sheet.getDataRange().getValues();
  const col = columnsOf(
    values[0].map(String),
    ["Location", "Scryfall ID", "Foil", "Condition", "Sold", "Pulled", "In box"],
    INVENTORY_TAB
  );
  const num = (v) => Number(v) || 0;
  const rows = values.slice(1).map((r, i) => ({
    row: i + 2,
    location: String(r[col["Location"]]),
    scryfallId: String(r[col["Scryfall ID"]]).trim(),
    finish: String(r[col["Foil"]]).trim().toLowerCase(),
    condition: String(r[col["Condition"]]),
    sold: num(r[col["Sold"]]),
    pulled: num(r[col["Pulled"]]),
    inBox: num(r[col["In box"]]),
  }));
  return {
    rows,
    add(row, field, delta) {
      const key = field === "Sold" ? "sold" : "pulled";
      const next = row[key] + delta;
      // Blank rather than 0, matching rows nobody has sold or pulled from.
      sheet.getRange(row.row, col[field] + 1).setValue(next === 0 ? "" : next);
      row[key] = next;
    },
  };
}

function ordersSheet(owner) {
  const ss = spreadsheet(owner);
  let sheet = ss.getSheetByName(ORDERS_TAB);
  if (!sheet) {
    sheet = ss.getSheetByName(LEGACY_ORDERS_TAB);
    if (sheet) sheet.setName(ORDERS_TAB);
  }
  if (!sheet) {
    sheet = ss.insertSheet(ORDERS_TAB);
    sheet.appendRow(ORDER_HEADERS);
    sheet.setFrozenRows(1);
    return sheet;
  }
  // A tab made by an older version of this script lacks the newest columns.
  const headers = sheet.getDataRange().getValues()[0].map(String);
  ORDER_HEADERS.filter((h) => headers.indexOf(h) < 0).forEach((h, i) => {
    sheet.getRange(1, headers.length + i + 1).setValue(h);
  });
  return sheet;
}

// {listing drop id: when dismissed}, pruned to the dashboard's window.
function readDismissed() {
  let dismissed = {};
  try {
    dismissed = JSON.parse(PropertiesService.getScriptProperties().getProperty("DISMISSED") || "{}");
  } catch (err) {
    dismissed = {};
  }
  const cutoff = Date.now() - 2 * RECENT_DAYS * 24 * 60 * 60 * 1000;
  for (const [id, when] of Object.entries(dismissed)) {
    if (new Date(when).getTime() < cutoff) delete dismissed[id];
  }
  return dismissed;
}

function readOrders(owner) {
  const sheet = ordersSheet(owner);
  const values = sheet.getDataRange().getValues();
  const col = columnsOf(values[0].map(String), ORDER_HEADERS, ORDERS_TAB);
  const iso = (v) => (v instanceof Date ? v.toISOString() : v || null);
  const orders = values.slice(1).map((r, i) => ({
    sheetRow: i + 2,
    owner,
    id: String(r[col["Order"]]),
    status: String(r[col["Status"]]),
    name: String(r[col["Card"]]),
    set: String(r[col["Set"]]),
    finish: String(r[col["Finish"]]),
    scryfallId: String(r[col["Scryfall ID"]]),
    qty: Number(r[col["Qty"]]) || 0,
    price: r[col["Price (USD)"]] === "" ? null : Number(r[col["Price (USD)"]]),
    location: String(r[col["Location"]]),
    inventoryRow: Number(r[col["Inventory row"]]) || null,
    offered: iso(r[col["Offered"]]),
    paid: iso(r[col["Paid"]]),
    closed: iso(r[col["Closed"]]),
    type: String(r[col["Type"]]) || "WTB",
    source: String(r[col["Source"]]),
    pulls: parsePulls(r[col["Location"]], r[col["Inventory row"]], Number(r[col["Qty"]]) || 0),
  }));
  return { sheet, col, orders };
}

function findOrder(owner, id) {
  const { sheet, col, orders } = readOrders(owner);
  const order = orders.find((o) => o.id === String(id));
  if (!order) throw new Error(`No order ${id} in ${owner}'s sheet`);
  return {
    order,
    sheet,
    update(fields) {
      for (const [name, value] of Object.entries(fields)) {
        sheet.getRange(order.sheetRow, col[name] + 1).setValue(value);
      }
    },
  };
}
