/**
 * LINEで「取得」と送ると GitHub Actions を起動して最新の市況通知を送らせる Webhook。
 * Cloudflare Workers（無料プラン）で動かす。
 *
 * 環境変数（Workers の Settings → Variables and Secrets。すべて Secret として登録）:
 *   LINE_CHANNEL_SECRET        LINE Developers ［チャネル基本設定］のチャネルシークレット
 *   LINE_CHANNEL_ACCESS_TOKEN  ［Messaging API設定］の長期トークン（GitHub Secretsと同じ値）
 *   GITHUB_PAT                 GitHubのFine-grained PAT（対象リポジトリのActions: Read and write のみ）
 *   GITHUB_REPO                "ユーザー名/リポジトリ名" 形式
 *   WORKFLOW_FILE              "morning-notify.yml"
 *   GITHUB_BRANCH              "main"（省略可）
 *
 * トリガーになる言葉は TRIGGERS を書き換えれば変えられる。
 */

const TRIGGERS = ["取得", "更新", "test"];

async function verifySignature(secret, body, signature) {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const mac = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(body));
  const expected = btoa(String.fromCharCode(...new Uint8Array(mac)));
  return expected === signature;
}

async function dispatchWorkflow(env) {
  const url =
    `https://api.github.com/repos/${env.GITHUB_REPO}` +
    `/actions/workflows/${env.WORKFLOW_FILE}/dispatches`;
  const r = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_PAT}`,
      Accept: "application/vnd.github+json",
      "User-Agent": "line-ondemand-trigger",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref: env.GITHUB_BRANCH || "main" }),
  });
  return r.status === 204 ? null : `${r.status} ${await r.text()}`;
}

async function reply(env, replyToken, text) {
  // Reply APIは無料枠の通数を消費しない
  await fetch("https://api.line.me/v2/bot/message/reply", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.LINE_CHANNEL_ACCESS_TOKEN}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ replyToken, messages: [{ type: "text", text }] }),
  });
}

export default {
  async fetch(request, env) {
    // LINE Developersの「検証」ボタンはGET/POST両方来るので、署名不一致以外は200を返す
    if (request.method !== "POST") return new Response("ok");

    const body = await request.text();
    const signature = request.headers.get("x-line-signature") || "";
    if (!(await verifySignature(env.LINE_CHANNEL_SECRET, body, signature))) {
      return new Response("bad signature", { status: 403 });
    }

    let payload;
    try {
      payload = JSON.parse(body);
    } catch {
      return new Response("ok");
    }

    for (const ev of payload.events ?? []) {
      if (ev.type !== "message" || ev.message?.type !== "text") continue;
      if (!TRIGGERS.includes(ev.message.text.trim())) continue;

      const err = await dispatchWorkflow(env);
      await reply(
        env,
        ev.replyToken,
        err === null
          ? "取得を開始しました。2〜4分ほどで届きます"
          : `GitHub Actionsの起動に失敗しました（${err.slice(0, 120)}）`,
      );
    }
    return new Response("ok");
  },
};
