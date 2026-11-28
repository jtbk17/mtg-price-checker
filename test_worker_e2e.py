"""End-to-end check of Add cards from Telegram: the real cloudflare_worker.js
in a browser, its Telegram API calls faked and its Apps Script calls
answered by the real google_apps_script.js against the fake spreadsheets in
test_orders_mocks.js — so sharing a ManaBox CSV to the bot, picking a sheet,
the rows landing in Inventory and Undo are tested together, along with the
feedback buttons and scheduled runs the Worker already handled.

Run with `python test_worker_e2e.py` (needs playwright, like
test_orders_e2e.py); exits non-zero if any check fails.
"""
import csv
import functools
import http.server
import io
import json
import sys
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8")
REPO = Path(__file__).parent

http.server.SimpleHTTPRequestHandler.log_message = lambda *a: None
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(REPO)))
BASE = f"http://127.0.0.1:{srv.server_address[1]}"
threading.Thread(target=srv.serve_forever, daemon=True).start()

ENV = {"TELEGRAM_BOT_TOKEN": "TOKEN", "TELEGRAM_WEBHOOK_SECRET": "sec", "SHEETS_PASSCODE": "pw",
       "TELEGRAM_CHAT_ID": "42", "GITHUB_REPO": "o/r", "GITHUB_TOKEN": "gh"}
CHAT = 42

# Telegram, GitHub and the Apps Script web app, faked inside the page.
FAKES = """
window.__tg = []; window.__gh = []; window.__files = {}; window.__webhook = { ok: true, result: { url: "https://w.example" } };
window.fetch = async (url, opts = {}) => {
  url = String(url);
  if (url.startsWith("https://script.google.com/")) return new Response(doPost({ postData: { contents: opts.body } }).s);
  if (url.startsWith("https://api.github.com/")) { __gh.push({ url, body: JSON.parse(opts.body || "{}") }); return new Response(null, { status: 204 }); }
  let m = url.match(/^https:\\/\\/api\\.telegram\\.org\\/file\\/botTOKEN\\/(.+)$/);
  if (m) return new Response(__files[m[1]]);
  m = url.match(/^https:\\/\\/api\\.telegram\\.org\\/botTOKEN\\/(\\w+)$/);
  if (m) {
    const body = JSON.parse(opts.body || "{}");
    __tg.push({ method: m[1], body });
    if (m[1] === "getFile") return Response.json({ ok: true, result: { file_path: body.file_id } });
    if (m[1] === "getWebhookInfo") return Response.json(__webhook);
    return Response.json({ ok: true, result: { message_id: 900 + __tg.length } });
  }
  throw new Error("unexpected fetch " + url);
};
window.__send = async (update, env) => {
  const worker = (await import("/cloudflare_worker.js")).default;
  const waits = [];
  const req = new Request("https://w.example/", { method: "POST", headers: { "X-Telegram-Bot-Api-Secret-Token": "sec" }, body: JSON.stringify(update) });
  const resp = await worker.fetch(req, env, { waitUntil: (p) => waits.push(p) });
  await Promise.all(waits);
  return resp.status;
};
window.__cron = async (cron, env) => {
  const worker = (await import("/cloudflare_worker.js")).default;
  const waits = [];
  await worker.scheduled({ cron }, env, { waitUntil: (p) => waits.push(p) });
  await Promise.all(waits);
};
"""

MANABOX = ["Name", "Set code", "Set name", "Collector number", "Foil", "Rarity", "Quantity", "ManaBox ID", "Scryfall ID",
           "Purchase price", "Misprint", "Altered", "Signed", "Condition", "Language", "Proxy", "Purchase price currency", "Added"]


def uid(n):
    return f"{n:08x}-0000-4000-8000-000000000000"


def mb(name, n, qty=1, price="1.50", added="2026-11-24T03:00:00.000000Z"):
    return [name, "msh", "Marvel Super Heroes", str(100 + n), "normal", "rare", str(qty), str(n), uid(n), price,
            "FALSE", "FALSE", "FALSE", "near_mint", "en", "FALSE", "USD", added]


def csv_text(rows, header=MANABOX):
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\r\n").writerows([header] + rows)
    return "﻿" + buf.getvalue()


results = []


def check(name, cond, extra=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


with sync_playwright() as p:
    try:
        b = p.chromium.launch()
    except Exception:
        b = p.chromium.launch(channel="msedge")
    pg = b.new_page()
    pg.route(f"{BASE}/worker.html", lambda r: r.fulfill(status=200, content_type="text/html",
             body='<html><body><script src="/test_orders_mocks.js"></script><script src="/google_apps_script.js"></script></body></html>'))
    pg.goto(f"{BASE}/worker.html")
    pg.add_script_tag(content=FAKES)

    msg_id = [100]

    def send(update, env=ENV):
        pg.evaluate("() => { __tg = []; __gh = []; }")
        status = pg.evaluate("([u, e]) => __send(u, e)", [update, env])
        return status, pg.evaluate("() => __tg"), pg.evaluate("() => __gh")

    def share(file_id, text, name="ManaBox_Inbox.csv", chat=CHAT, env=ENV, mime="text/csv"):
        pg.evaluate("([k, v]) => { __files[k] = v; }", [file_id, text])
        msg_id[0] += 1
        doc = {"message_id": msg_id[0], "chat": {"id": chat}, "document": {"file_id": file_id, "file_name": name, "mime_type": mime}}
        return doc, send({"message": doc}, env)

    def tap(data, doc=None, chat=CHAT, text="", env=ENV):
        message = {"message_id": 555, "chat": {"id": chat}, "text": text}
        if doc:
            message["reply_to_message"] = doc
        return send({"callback_query": {"id": "cb1", "data": data, "message": message}}, env)

    def calls(tg, method):
        return [c["body"] for c in tg if c["method"] == method]

    def inv_len(book):
        return pg.evaluate("b => __books[b].Inventory.rows.length", book)

    batch = [mb("Thor, God of Thunder", 1, price="16.99"), mb("Castle Doom", 2, qty=2), mb("Big Slime", 3, price="")]

    # ---- who may log cards ----
    no_chat = {k: v for k, v in ENV.items() if k != "TELEGRAM_CHAT_ID"}
    s_before = inv_len("S")
    _, (status, tg, _) = share("f0", csv_text(batch), env=no_chat)
    sent = calls(tg, "sendMessage")
    check("Telegram gets its OK", status == 200)
    check("no chat set yet: the bot says which id to set, logs nothing",
          len(sent) == 1 and "TELEGRAM_CHAT_ID" in sent[0]["text"] and "<code>42</code>" in sent[0]["text"] and inv_len("S") == s_before, sent)
    _, (_, tg, _) = share("f0", csv_text(batch), chat=7)
    check("another chat is ignored", tg == [], tg)
    _, (_, tg, _) = share("photo1", "hello", name="photo.jpg", mime="image/jpeg")
    check("a file that isn't a CSV: says what to send", "Send a ManaBox CSV export" in calls(tg, "sendMessage")[0]["text"])
    _, (_, tg, _) = share("bad", csv_text([["a", "b"]], header=["Card", "Count"]), name="notes.csv")
    check("a CSV that isn't from ManaBox: says so", "doesn't look like a ManaBox CSV export" in calls(tg, "sendMessage")[0]["text"])

    # ---- share, pick a sheet, logged ----
    doc, (_, tg, _) = share("f1", csv_text(batch))
    ask = calls(tg, "sendMessage")[0]
    buttons = [btn for row in ask["reply_markup"]["inline_keyboard"] for btn in row]
    check("asks which sheet, with what's in the file", "3 rows" in ask["text"] and "4 cards" in ask["text"] and "bought for $19.99" in ask["text"], ask["text"])
    check("...as a reply to the file, a button per sheet showing its next slot",
          ask["reply_to_message_id"] == doc["message_id"]
          and [(x["text"], x["callback_data"]) for x in buttons] == [("Shared → B05-04", "in:Shared"), ("Joel → B02-02", "in:Joel"), ("Cancel", "inx")], buttons)
    status, tg, _ = tap("in:Shared", doc=doc)
    edits = calls(tg, "editMessageText")
    check("tap: buttons off at once, so a second tap can't log it twice",
          edits[0]["text"].startswith("⏳") and edits[0]["reply_markup"]["inline_keyboard"] == [] and calls(tg, "answerCallbackQuery"))
    done = edits[-1]
    check("logged: where to put them, with Undo",
          "Logged 4 cards behind <b>B05-04</b> in Shared's sheet" in done["text"] and "move Inbox to Collection" in done["text"]
          and done["reply_markup"]["inline_keyboard"][0][0]["text"] == "Undo", done)
    rows = pg.evaluate("() => __books.S.Inventory.rows.slice(-3).map(r => [r[0], r[1], r[9]])")
    check("rows in Shared's Inventory behind B05-04", inv_len("S") == s_before + 3
          and rows == [["B05-04", "Thor, God of Thunder", 1], ["B05-04", "Castle Doom", 2], ["B05-04", "Big Slime", 1]], rows)

    doc2, (_, tg, _) = share("f2", csv_text(batch))
    again = calls(tg, "sendMessage")[0]
    check("same file again: already logged, no buttons", "already logged in Shared's sheet" in again["text"] and "reply_markup" not in again, again)
    _, (_, tg, _) = share("f3", csv_text([mb("Big Slime", 3), mb("New One", 9, added="2026-11-24T05:00:00Z")]))
    part = calls(tg, "sendMessage")[0]["text"]
    check("Inbox not emptied: which cards and what to do", "1 card in it was already logged" in part and "move them from Inbox to Collection" in part, part)
    _, (_, tg, _) = share("f4", csv_text([mb("Huge", 11, qty=150, added="2026-11-24T06:00:00Z")]))
    check("bigger than a slot: warned, still offered", "More than a slot holds (100)" in calls(tg, "sendMessage")[0]["text"])

    # ---- undo, with a confirm step ----
    undo = done["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    _, tg, _ = tap(undo, text="Logged…")
    confirm = calls(tg, "editMessageReplyMarkup")[0]["reply_markup"]["inline_keyboard"][0]
    check("Undo asks first", [x["text"] for x in confirm] == ["Yes, take them out", "Keep"] and inv_len("S") == s_before + 3)
    _, tg, _ = tap(confirm[1]["callback_data"])
    check("Keep puts the Undo button back", calls(tg, "editMessageReplyMarkup")[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == undo)
    _, tg, _ = tap(undo.replace("iu:", "iy:", 1), chat=7)
    check("another chat can't undo", inv_len("S") == s_before + 3 and not calls(tg, "editMessageText"))
    _, tg, _ = tap(confirm[0]["callback_data"], text="Logged…")
    check("Yes: the batch is taken back out", inv_len("S") == s_before and "Undone" in calls(tg, "editMessageText")[-1]["text"])
    _, tg, _ = tap(confirm[0]["callback_data"], text="Logged 4 cards")
    check("undoing twice says why not", "Only the latest batch can be undone" in calls(tg, "editMessageText")[-1]["text"])

    _, tg, _ = tap("in:Joel", doc=doc2)
    check("an undone file can go in again (here to Joel's sheet)", "behind <b>B02-02</b> in Joel's sheet" in calls(tg, "editMessageText")[-1]["text"])
    rescan = [r[:17] + ["2026-11-28T08:07:25Z"] for r in batch]
    doc6, _ = share("f6", csv_text(rescan))
    _, tg, _ = tap("in:Joel", doc=doc6)
    ask_again = calls(tg, "editMessageText")[-1]
    again_buttons = [b["callback_data"] for row in ask_again["reply_markup"]["inline_keyboard"] for b in row]
    j_rows = inv_len("J")
    check("same cards rescanned: asks, with Log anyway", "same 4 cards as the batch behind B02-02" in ask_again["text"]
          and again_buttons == ["inf:Joel", "inx"], ask_again)
    _, tg, _ = tap("inf:Joel", doc=doc6)
    check("Log anyway logs them", "Logged 4 cards behind <b>B02-03</b>" in calls(tg, "editMessageText")[-1]["text"] and inv_len("J") == j_rows + 3)
    _, tg, _ = tap("in:Shared", doc=None)
    check("a tap that lost its file says to send it again", "send it again" in calls(tg, "editMessageText")[-1]["text"])
    _, tg, _ = tap("inx")
    check("Cancel: nothing logged", calls(tg, "editMessageText")[-1]["text"].startswith("Cancelled"))
    bad_pw = dict(ENV, SHEETS_PASSCODE="wrong")
    doc5, _ = share("f5", csv_text([mb("Pw Card", 12, added="2026-11-24T07:00:00Z")]))
    _, tg, _ = tap("in:Shared", doc=doc5, env=bad_pw)
    check("a wrong passcode on the Worker is reported, not swallowed", "Wrong passcode" in calls(tg, "editMessageText")[-1]["text"])

    # ---- what the Worker already did still works ----
    _, tg, gh = tap("fb:rec7:good")
    check("feedback buttons still record feedback",
          gh and gh[0]["body"]["client_payload"] == {"rec_id": "rec7", "verdict": "good"} and calls(tg, "answerCallbackQuery"), gh)
    pg.evaluate("() => { __tg = []; __gh = []; __webhook = { ok: true, result: { url: 'https://w.example', allowed_updates: ['callback_query'] } }; }")
    pg.evaluate("e => __cron('*/30 * * * *', e)", ENV)
    tg, gh = pg.evaluate("() => __tg"), pg.evaluate("() => __gh")
    hook = calls(tg, "setWebhook")
    check("cron still starts its workflows", sorted(g["url"].split("/")[-2] for g in gh) == ["auction-deals.yml", "sheet-sync.yml"], gh)
    check("webhook limited to button taps: messages added",
          hook == [{"url": "https://w.example", "secret_token": "sec", "allowed_updates": ["callback_query", "message"]}], hook)
    pg.evaluate("() => { __tg = []; __webhook = { ok: true, result: { url: 'https://w.example' } }; }")
    pg.evaluate("e => __cron('0 14 * * *', e)", ENV)
    check("webhook already getting everything: left alone", not calls(pg.evaluate("() => __tg"), "setWebhook"))
    b.close()
srv.shutdown()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
