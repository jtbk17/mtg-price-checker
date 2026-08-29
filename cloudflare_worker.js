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
//   TELEGRAM_BOT_TOKEN  (secret) - same bot token used elsewhere
//   GITHUB_TOKEN        (secret) - a GitHub token with Contents: write on this repo
//   GITHUB_REPO         (var)    - "owner/repo", e.g. "jtbk17/mtg-price-checker"

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("OK");
    }

    let update;
    try {
      update = await request.json();
    } catch (err) {
      return new Response("OK");
    }

    const callback = update.callback_query;
    if (!callback) {
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
    const originalText = callback.message?.text || "";
    const mark = feedback === "good" ? "✅ Marked: good pick" : "❌ Marked: false positive";

    console.log("TELEGRAM_BOT_TOKEN present:", !!env.TELEGRAM_BOT_TOKEN, "length:", (env.TELEGRAM_BOT_TOKEN || "").length);
    console.log("GITHUB_TOKEN present:", !!env.GITHUB_TOKEN, "GITHUB_REPO:", env.GITHUB_REPO);
    console.log("chatId:", chatId, "messageId:", messageId, "recId:", recId, "feedback:", feedback);

    const telegramApi = `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}`;

    async function loggedFetch(label, url, options) {
      try {
        const resp = await fetch(url, options);
        const bodyText = await resp.text();
        console.log(`${label} -> status ${resp.status}: ${bodyText.slice(0, 300)}`);
        return resp;
      } catch (err) {
        console.log(`${label} -> threw error: ${err.message}`);
        throw err;
      }
    }

    const tasks = [
      loggedFetch("answerCallbackQuery", `${telegramApi}/answerCallbackQuery`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          callback_query_id: callback.id,
          text: feedback === "good" ? "Thanks — noted!" : "Thanks — marked as false positive",
        }),
      }),
      loggedFetch("githubDispatch", `https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`, {
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
      tasks.push(
        loggedFetch("editMessageText", `${telegramApi}/editMessageText`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            chat_id: chatId,
            message_id: messageId,
            text: `${originalText}\n\n${mark}`,
            parse_mode: "HTML",
            reply_markup: { inline_keyboard: [] },
          }),
        })
      );
    } else {
      console.log("Skipping editMessageText: missing chatId or messageId");
    }

    await Promise.allSettled(tasks);
    return new Response("OK");
  },
};
