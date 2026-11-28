// Cloudflare Worker: receives Telegram's webhook the instant a feedback
// button is tapped, immediately acknowledges it and edits the message
// (this is the whole point — a live endpoint is the only way Telegram can
// notify anything the moment a tap happens, rather than on a delay), then
// fires a GitHub repository_dispatch event to actually record the
// feedback (see record_feedback.py / telegram-webhook-feedback.yml).
//
// Deploy via the Cloudflare dashboard (Workers & Pages -> Create -> paste
// this file's contents into the editor). Required secrets/vars, set under
// the Worker's Settings -> Variables:
//   TELEGRAM_BOT_TOKEN     (secret) - same bot token used elsewhere
//   TELEGRAM_WEBHOOK_SECRET(secret) - random string of your choosing; also
//                                     pass it as secret_token when calling
//                                     setWebhook (see below) so Telegram
//                                     echoes it back on every real update
//   GITHUB_TOKEN           (secret) - a GitHub token with Contents: write and
//                                     Actions: write on this repo
//   GITHUB_REPO            (var)    - "owner/repo", e.g. "jtbk17/mtg-price-checker"
//   SHEETS_PASSCODE        (secret) - the Apps Script web app's PASSCODE, for
//                                     Add cards from Telegram (below)
//   TELEGRAM_CHAT_ID       (var)    - the only chat allowed to log cards; the
//                                     bot replies with it the first time a
//                                     file arrives while it's unset
//
// It also starts the scheduled GitHub workflows on time (scheduled()
// below), since GitHub's own cron runs them hours late. Add these under
// the Worker's Settings -> Triggers -> Cron Triggers:
//   */30 * * * *         shop/auction check and sheet sync, every 30 minutes
//   0 0-13,15-23 * * *   marketplace refresh, hourly except 10pm SGT (the
//                        nightly's hour: starting both at once could bump a
//                        waiting Telegram-feedback run out of their queue)
//   0 14 * * *           nightly refresh, 10pm SGT
//
// Register the webhook with the matching secret_token (one-time, from a
// shell — replace <bot-token> and <secret>):
//   curl "https://api.telegram.org/bot<bot-token>/setWebhook" \
//     -d "url=https://<your-worker>.workers.dev" \
//     -d "secret_token=<secret>"
//
// Without this, anyone who finds the Worker's URL can forge a Telegram
// callback payload and trigger a repository_dispatch (which runs Actions
// and commits to the repo) or spoof feedback — Telegram signs every real
// webhook call by echoing secret_token back in a header, so checking it
// below is what actually verifies the request's origin.

// ---- Add cards from Telegram: share a ManaBox CSV export to the bot, pick
// the sheet (send /box to set how many slots a box holds), and it's logged through the same Apps Script web app the
// dashboard's Add cards uses (google_apps_script.js) — next empty slot,
// the same duplicate guard, and an Undo. ----

const SHEETS_API_URL = "https://script.google.com/macros/s/AKfycbyHzEPynfBNbjd6_ZawrW47MmlicWc0BZuznQYO-wJmZ5l49v0enPtAK6tbz0EUzXyT/exec";

// Kept in step with docs/index.html's parseCsv / manaboxRows.
function parseCsv(text) {
  const rows = [];
  let row = [], field = "", quoted = false;
  text = text.replace(/^\uFEFF/, "");
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') { field += '"'; i++; }
      else if (c === '"') quoted = false;
      else field += c;
    } else if (c === '"') quoted = true;
    else if (c === ",") { row.push(field); field = ""; }
    else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i++;
      row.push(field); rows.push(row); row = []; field = "";
    } else field += c;
  }
  if (field !== "" || row.length) { row.push(field); rows.push(row); }
  return rows.filter((r) => r.some((v) => v.trim() !== ""));
}

const MANABOX_COLUMNS = {
  name: "Name", setCode: "Set code", setName: "Set name", collector: "Collector number", foil: "Foil",
  rarity: "Rarity", qty: "Quantity", scryfallId: "Scryfall ID", price: "Purchase price",
  condition: "Condition", language: "Language", added: "Added",
};

function manaboxRows(text) {
  const [header, ...lines] = parseCsv(text);
  if (!header) throw new Error("The file is empty");
  const at = {};
  for (const [key, column] of Object.entries(MANABOX_COLUMNS)) at[key] = header.findIndex((h) => h.trim() === column);
  const missing = ["name", "qty", "scryfallId"].filter((k) => at[k] < 0).map((k) => MANABOX_COLUMNS[k]);
  if (missing.length) throw new Error(`This doesn't look like a ManaBox CSV export (no ${missing.join(", ")} column)`);
  if (!lines.length) throw new Error("The file has no cards in it");
  return lines.map((line, i) => {
    const row = {};
    for (const key of Object.keys(MANABOX_COLUMNS)) row[key] = at[key] >= 0 ? (line[at[key]] || "").trim() : "";
    row.qty = Number(row.qty);
    if (!row.name || !row.scryfallId || !Number.isInteger(row.qty) || row.qty < 1) {
      throw new Error(`Row ${i + 2}${row.name ? ` (${row.name})` : ""} is missing its name, Scryfall ID or quantity`);
    }
    return row;
  });
}

const escapeHtml = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const plural = (n) => `${n} card${n === 1 ? "" : "s"}`;
const money = (n) => `$${n.toFixed(2)}`;

async function tg(env, method, body) {
  const resp = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return resp.json();
}

// The Apps Script web app, as the dashboard calls it (text/plain body).
async function sheets(env, action, params = {}) {
  const resp = await fetch(env.SHEETS_API_URL || SHEETS_API_URL, {
    method: "POST",
    body: JSON.stringify({ action, passcode: env.SHEETS_PASSCODE, ...params }),
  });
  const data = await resp.json();
  if (!data.ok) throw Object.assign(new Error(data.error || "The sheets didn't answer"), { data });
  return data;
}

async function readCsv(env, fileId) {
  const file = await tg(env, "getFile", { file_id: fileId });
  if (!file.ok) throw new Error("Couldn't fetch the file from Telegram — send it again");
  const resp = await fetch(`https://api.telegram.org/file/bot${env.TELEGRAM_BOT_TOKEN}/${file.result.file_path}`);
  return manaboxRows(await resp.text());
}

// Cards in this file some sheet has already logged (by ManaBox's Added stamp).
function alreadyLogged(rows, status) {
  for (const [owner, st] of Object.entries(status)) {
    const seen = new Set(st.added || []);
    const n = rows.filter((r) => r.added && seen.has(r.added)).reduce((k, r) => k + r.qty, 0);
    if (n) return { owner, n };
  }
  return null;
}

// Only the chat the alerts go to may log cards. Until TELEGRAM_CHAT_ID is
// set on the Worker, a file just gets back this chat's id to set it to.
function allowedChat(env, chatId) {
  return env.TELEGRAM_CHAT_ID && String(chatId) === String(env.TELEGRAM_CHAT_ID).trim();
}

// Buttons under a logged batch: Undo, and moving it across a box boundary
// when it doesn't fit (or the previous box had room after all).
function loggedKeyboard(owner, batch, status) {
  const rows = [[{ text: "Undo", callback_data: `iu:${owner}:${batch}` }]];
  const refit = status && status.refit;
  if (refit && refit.batch === batch) {
    rows.push([{
      text: refit.fits === "next" ? `Doesn't fit → ${refit.to}` : `Fits in ${refit.to.slice(0, 3)} → ${refit.to}`,
      callback_data: `mv:${owner}:${batch}`,
    }]);
  }
  return rows;
}

const whereToPut = (cards, location, owner) =>
  `✅ ${plural(cards)} behind <b>${escapeHtml(location)}</b> in ${escapeHtml(owner)}'s sheet.\n`
  + `Write ${escapeHtml(location)} on the divider and box them, then in ManaBox move Inbox to Collection.`;

// /box: each sheet's box being filled, with -1 / +1 on how many slots it holds.
function boxMessage(status) {
  const lines = [];
  const keyboard = [];
  for (const [owner, st] of Object.entries(status)) {
    if (!st.box) throw new Error("Update the Apps Script to set slots per box");
    const { name, slots, used } = st.box;
    lines.push(`<b>${escapeHtml(owner)}</b> — box ${name} holds <b>${slots}</b> slots (${used} used) · next ${st.next}`);
    const row = [];
    if (slots > Math.max(used, 1)) row.push({ text: `${owner} ${name}: ${slots - 1}`, callback_data: `bx:${owner}:${name}:${slots - 1}` });
    if (slots < 99) row.push({ text: `${owner} ${name}: ${slots + 1}`, callback_data: `bx:${owner}:${name}:${slots + 1}` });
    keyboard.push(row);
  }
  return { text: `📦 ${lines.join("\n")}\n\nTap how many slots the box holds.`, keyboard };
}

async function handleMessage(message, env) {
  const doc = message.document;
  const chatId = message.chat && message.chat.id;
  if (chatId == null) return;
  if (!doc && /^\/box(es)?(@\w+)?(\s|$)/i.test(message.text || "")) {
    if (!allowedChat(env, chatId)) return;
    try {
      const { text, keyboard } = boxMessage((await sheets(env, "intakeStatus")).intake);
      await tg(env, "sendMessage", { chat_id: chatId, text, parse_mode: "HTML", reply_markup: { inline_keyboard: keyboard } });
    } catch (err) {
      await tg(env, "sendMessage", { chat_id: chatId, text: `❌ ${err.message}` });
    }
    return;
  }
  if (!doc) return;
  const reply = (text, extra = {}) => tg(env, "sendMessage", {
    chat_id: chatId, text, parse_mode: "HTML", reply_to_message_id: message.message_id, ...extra,
  });
  if (!env.TELEGRAM_CHAT_ID) {
    await reply(`To log cards from this chat, add a variable <b>TELEGRAM_CHAT_ID</b> = <code>${escapeHtml(chatId)}</code> to the Cloudflare Worker (Settings → Variables), then send the file again.`);
    return;
  }
  if (!allowedChat(env, chatId)) return;
  if (!/\.csv$/i.test(doc.file_name || "") && !/csv/i.test(doc.mime_type || "")) {
    await reply("Send a ManaBox CSV export (Export → CSV → Share → Telegram) to log cards.");
    return;
  }
  let rows, status;
  try {
    [rows, status] = await Promise.all([readCsv(env, doc.file_id), sheets(env, "intakeStatus").then((d) => d.intake)]);
  } catch (err) {
    await reply(`❌ ${escapeHtml(err.message)}`);
    return;
  }
  const cards = rows.reduce((n, r) => n + r.qty, 0);
  const value = rows.reduce((v, r) => v + (Number(r.price) || 0) * r.qty, 0);
  const summary = `📥 <b>${escapeHtml(doc.file_name || "ManaBox export")}</b>\n${plural(cards)}${rows.length !== cards ? ` in ${rows.length} rows` : ""}${value ? ` · bought for ${money(value)}` : ""}`;
  const dup = alreadyLogged(rows, status);
  if (dup) {
    await reply(`${summary}\n\n❌ ${dup.n === cards ? "This file was already logged" : `${plural(dup.n)} in it ${dup.n === 1 ? "was" : "were"} already logged`} in ${escapeHtml(dup.owner)}'s sheet.`
      + (dup.n === cards ? "" : " In ManaBox, move them from Inbox to Collection, then export and send it again."));
    return;
  }
  const owners = Object.keys(status);
  const big = owners.some((o) => cards > status[o].cardsPerSlot);
  await reply(`${summary}${big ? `\n⚠ More than a slot holds (${status[owners[0]].cardsPerSlot}) — fine if they fit behind the divider.` : ""}\n\nLog it in which sheet?`, {
    reply_markup: {
      inline_keyboard: [
        owners.map((o) => ({ text: `${o} → ${status[o].next}`, callback_data: `in:${o}` })),
        [{ text: "Cancel", callback_data: "inx" }],
      ],
    },
  });
}

async function handleIntakeTap(callback, env) {
  const message = callback.message || {};
  const chatId = message.chat && message.chat.id;
  const answer = (text) => tg(env, "answerCallbackQuery", { callback_query_id: callback.id, ...(text ? { text } : {}) });
  if (!allowedChat(env, chatId)) {
    await answer();
    return;
  }
  const edit = (text, keyboard = []) => tg(env, "editMessageText", {
    chat_id: chatId, message_id: message.message_id, text, parse_mode: "HTML", reply_markup: { inline_keyboard: keyboard },
  });
  const keyboard = (rows) => tg(env, "editMessageReplyMarkup", {
    chat_id: chatId, message_id: message.message_id, reply_markup: { inline_keyboard: rows },
  });
  const [kind, owner, batch, count] = (callback.data || "").split(":");

  if (kind === "inx") {
    await Promise.all([answer(), edit("Cancelled — nothing logged.")]);
  } else if (kind === "in" || kind === "inf") {
    const doc = message.reply_to_message && message.reply_to_message.document;
    // Buttons off first, so a second tap can't log it twice.
    await Promise.all([answer("Logging…"), edit(`⏳ Logging in ${escapeHtml(owner)}'s sheet…`)]);
    try {
      if (!doc) throw new Error("Couldn't find the file this was for — send it again");
      const [rows, status] = await Promise.all([readCsv(env, doc.file_id), sheets(env, "intakeStatus").then((d) => d.intake)]);
      if (!status[owner]) throw new Error(`No sheet set up for ${owner}`);
      const slot = status[owner].next;
      const data = await sheets(env, "intake", { owner, location: slot, rows, force: kind === "inf" });
      const done = data.logged;
      await edit(whereToPut(done.cards, done.location, owner).replace("✅ ", "✅ Logged "),
        loggedKeyboard(owner, done.batch, data.intake && data.intake[owner]));
    } catch (err) {
      // The same cards as an earlier batch: ask before logging them again.
      if (err.data && err.data.sameAs) {
        await edit(`⚠️ ${escapeHtml(err.message)}`, [[
          { text: "Log anyway", callback_data: `inf:${owner}` },
          { text: "Cancel", callback_data: "inx" },
        ]]);
      } else {
        await edit(`❌ ${escapeHtml(err.message)}`);
      }
    }
  } else if (kind === "iu") {
    await Promise.all([answer(), keyboard([[
      { text: "Yes, take them out", callback_data: `iy:${owner}:${batch}` },
      { text: "Keep", callback_data: `ik:${owner}:${batch}` },
    ]])]);
  } else if (kind === "ik") {
    await answer();
    const status = await sheets(env, "intakeStatus").then((d) => d.intake[owner]).catch(() => null);
    await keyboard(loggedKeyboard(owner, batch, status));
  } else if (kind === "mv") {
    // Buttons off first, so a double tap can't move it twice.
    await Promise.all([answer("Moving…"), keyboard([])]);
    try {
      const data = await sheets(env, "refitBatch", { owner, batch });
      const m = data.moved;
      await edit(`${whereToPut(m.cards, m.to, owner)}\n📦 Box ${m.box} now holds ${m.slots} slots.`,
        loggedKeyboard(owner, batch, data.intake[owner]));
    } catch (err) {
      const status = await sheets(env, "intakeStatus").then((d) => d.intake[owner]).catch(() => null);
      await edit(`${escapeHtml(message.text || "")}\n\n❌ ${escapeHtml(err.message)}`, loggedKeyboard(owner, batch, status));
    }
  } else if (kind === "bx") {
    // Here `batch` is the box (B02) and `count` its new number of slots.
    await answer("Saving…");
    try {
      const data = await sheets(env, "setBoxSlots", { owner, box: batch, slots: Number(count) });
      const { text, keyboard: rows } = boxMessage(data.intake);
      await edit(text, rows);
    } catch (err) {
      await edit(`❌ ${escapeHtml(err.message)} — send /box again.`);
    }
  } else if (kind === "iy") {
    await answer("Undoing…");
    try {
      await sheets(env, "undoIntake", { owner, batch });
      await edit(`↩️ Undone — that batch is out of ${escapeHtml(owner)}'s sheet again.`);
    } catch (err) {
      await edit(`${escapeHtml(message.text || "")}\n\n❌ ${escapeHtml(err.message)}`);
    }
  } else {
    await answer();
  }
}

// The webhook only gets the update types it was registered for. If it was
// limited to button taps, add messages, so files shared to the bot arrive.
async function ensureWebhookGetsMessages(env) {
  if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_WEBHOOK_SECRET) return;
  const info = await tg(env, "getWebhookInfo", {});
  const allowed = info.ok && info.result.allowed_updates;
  if (!info.ok || !info.result.url || !allowed || !allowed.length || allowed.includes("message")) return;
  await tg(env, "setWebhook", {
    url: info.result.url, secret_token: env.TELEGRAM_WEBHOOK_SECRET, allowed_updates: [...allowed, "message"],
  });
}

// Cron trigger -> the workflow files it starts.
const SCHEDULES = {
  "*/30 * * * *": ["auction-deals.yml", "sheet-sync.yml"],
  "0 0-13,15-23 * * *": ["refresh-marketplace.yml"],
  "0 14 * * *": ["nightly-refresh.yml"],
};

export default {
  async scheduled(event, env, ctx) {
    const workflows = SCHEDULES[event.cron] || [];
    ctx.waitUntil(ensureWebhookGetsMessages(env).catch((err) => console.log(`Webhook check failed: ${err}`)));
    ctx.waitUntil(
      Promise.allSettled(
        workflows.map((file) =>
          fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/${file}/dispatches`, {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              Authorization: `Bearer ${env.GITHUB_TOKEN}`,
              Accept: "application/vnd.github+json",
              "User-Agent": "mtg-price-checker-worker",
            },
            body: JSON.stringify({ ref: "main" }),
          }).then((resp) => {
            if (!resp.ok) console.log(`Starting ${file} failed: ${resp.status}`);
          })
        )
      )
    );
  },

  async fetch(request, env, ctx) {
    if (request.method !== "POST") {
      return new Response("OK");
    }

    if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.TELEGRAM_WEBHOOK_SECRET) {
      return new Response("Forbidden", { status: 403 });
    }

    let update;
    try {
      update = await request.json();
    } catch (err) {
      return new Response("OK");
    }

    // A file shared to the bot, or a tap on Add cards' buttons: answered in
    // the background (the sheets take a few seconds), so Telegram gets its OK
    // straight away.
    if (update.message) {
      ctx.waitUntil(handleMessage(update.message, env).catch((err) => console.log(`Add cards failed: ${err}`)));
      return new Response("OK");
    }

    const callback = update.callback_query;
    if (!callback) {
      return new Response("OK");
    }
    if (/^(in|inf|inx|iu|ik|iy|mv|bx)(:|$)/.test(callback.data || "")) {
      ctx.waitUntil(handleIntakeTap(callback, env).catch((err) => console.log(`Add cards failed: ${err}`)));
      return new Response("OK");
    }

    const parts = (callback.data || "").split(":");
    if (parts.length !== 3 || parts[0] !== "fb") {
      return new Response("OK");
    }
    const [, recId, verdict] = parts;
    const feedback = verdict === "good" ? "good" : "bad";

    const chatId = callback.message?.chat?.id;
    const messageId = callback.message?.message_id;
    // Market alerts are sent as photos (image + caption) so you can see
    // which printing is trending; a photo message can only be edited via
    // editMessageCaption, never editMessageText, and vice versa for a
    // plain text message — pick based on which one Telegram sent back.
    const isPhoto = callback.message?.caption !== undefined;
    const originalBody = isPhoto ? callback.message?.caption : callback.message?.text || "";
    const mark = feedback === "good" ? "✅ Marked: good pick" : "❌ Marked: false positive";

    const telegramApi = `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}`;

    const tasks = [
      fetch(`${telegramApi}/answerCallbackQuery`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          callback_query_id: callback.id,
          text: feedback === "good" ? "Thanks — noted!" : "Thanks — marked as false positive",
        }),
      }),
      fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${env.GITHUB_TOKEN}`,
          Accept: "application/vnd.github+json",
          "User-Agent": "mtg-price-checker-worker",
        },
        body: JSON.stringify({
          event_type: "telegram_feedback",
          client_payload: { rec_id: recId, verdict: feedback },
        }),
      }),
    ];

    if (chatId && messageId) {
      const editMethod = isPhoto ? "editMessageCaption" : "editMessageText";
      const editBody = {
        chat_id: chatId,
        message_id: messageId,
        parse_mode: "HTML",
        reply_markup: { inline_keyboard: [] },
      };
      editBody[isPhoto ? "caption" : "text"] = `${originalBody}\n\n${mark}`;

      tasks.push(
        fetch(`${telegramApi}/${editMethod}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(editBody),
        })
      );
    }

    await Promise.allSettled(tasks);
    return new Response("OK");
  },
};
