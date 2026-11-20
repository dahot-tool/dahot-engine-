// engine-proxy Edge Function — Dahot-Tool (hxqkgmmuaflghfwcqiez)
// Saved from the live dashboard source reported 2026-11-08 ~20:06 PKT
// (deployment count 47; "playlist" action added that day). Verify JWT: ON.
// Secrets used: ENGINE_URL, ENGINE_URL_YT, DT_ENGINE_KEY, SEARCH_API_KEY,
// SEARCH_CX (env only, never in code).

import { serve } from "https://deno.land/std@0.224.0/http/server.ts";

const ENGINE_URL = Deno.env.get("ENGINE_URL") ?? "";
const ENGINE_URL_YT = Deno.env.get("ENGINE_URL_YT") ?? "";
const ENGINE_KEY = Deno.env.get("DT_ENGINE_KEY") ?? "";

// YouTube links are served by the dedicated YouTube engine when one is
// configured: the main engine's host is refused by the public media
// proxy YouTube downloads now route through, while the YouTube
// engine's host is accepted (both proven live 2026-11-10). Every
// other platform keeps using the main engine, so a failure of the
// YouTube engine can only ever affect YouTube.
function engineFor(target: string): string {
  if (!ENGINE_URL_YT || !target) return ENGINE_URL;
  try {
    const h = new URL(target).hostname.toLowerCase();
    if (h === "youtu.be" || h === "youtube.com" || h.endsWith(".youtube.com")) return ENGINE_URL_YT;
  } catch { /* not a parseable URL — main engine */ }
  return ENGINE_URL;
}

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
  "Access-Control-Expose-Headers": "Content-Disposition, Content-Type, Content-Length",
};

// ---- Naam-search daily caps (limits-protection plan, step 3, 2026-11-12) ----
// Serper's free grant is a ONE-TIME 2,500 searches, so psearch counts every
// successful search in public.psearch_daily (user_key, day, count): a
// per-caller cap of 10/day and a global cap of 250/day (Asia/Karachi day).
// A capped caller gets the same response shape the site already treats as
// "credits exhausted", so the tab quietly returns to link-only mode — the
// owner's standing fallback. Every counter operation is best-effort: a
// counter failure never breaks a search.
const PS_USER_CAP = 10;
const PS_GLOBAL_CAP = 250;
const PS_SB_URL = Deno.env.get("SUPABASE_URL") ?? "";
const PS_SB_SVC = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
const PS_SB_ANON = Deno.env.get("SUPABASE_ANON_KEY") ?? "";
function psHeaders() { return { "apikey": PS_SB_SVC, "Authorization": "Bearer " + PS_SB_SVC, "Content-Type": "application/json" }; }
function psDay(): string {
  try { return new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Karachi" }).format(new Date()); } catch { return new Date().toISOString().slice(0, 10); }
}
async function psCallerKey(req: Request): Promise<string> {
  const auth = req.headers.get("Authorization") ?? "";
  if (auth && PS_SB_URL && PS_SB_ANON) {
    try {
      const r = await fetch(PS_SB_URL + "/auth/v1/user", { headers: { "apikey": PS_SB_ANON, "Authorization": auth } });
      if (r.ok) { const u = await r.json(); if (u && u.id) return "u:" + u.id; }
    } catch { /* fall through to IP */ }
  }
  const ip = (req.headers.get("x-forwarded-for") ?? "").split(",")[0].trim();
  return "ip:" + (ip || "unknown");
}
async function psCounts(day: string, key: string): Promise<{ user: number; global: number } | null> {
  if (!PS_SB_URL || !PS_SB_SVC) return null;
  try {
    const r = await fetch(PS_SB_URL + "/rest/v1/psearch_daily?day=eq." + encodeURIComponent(day) + "&select=user_key,count", { headers: psHeaders() });
    if (!r.ok) return null;
    const rows = await r.json().catch(() => null);
    if (!Array.isArray(rows)) return null;
    let user = 0, global = 0;
    for (const row of rows) { const c = Number(row.count) || 0; global += c; if (row.user_key === key) user = c; }
    return { user, global };
  } catch { return null; }
}
async function psBump(day: string, key: string, current: number): Promise<void> {
  if (!PS_SB_URL || !PS_SB_SVC) return;
  try {
    await fetch(PS_SB_URL + "/rest/v1/psearch_daily", { method: "POST", headers: { ...psHeaders(), "Prefer": "resolution=merge-duplicates" }, body: JSON.stringify({ user_key: key, day: day, count: current + 1 }) });
  } catch { /* best effort */ }
}

serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  try {
    const url = new URL(req.url);

    // "convert" is handled BEFORE any JSON parsing: the request body is the
    // raw uploaded file (Video -> MP3 / Video -> GIF tools). The site calls
    // ?action=convert&mode=mp3|gif&name=<filename>&start=<s>&dur=<s> with the
    // file bytes as the body; we pass them straight to the engine and stream
    // the converted file back as an attachment.
    if (url.searchParams.get("action") === "convert") {
      const qs = new URLSearchParams();
      for (const k of ["mode", "name", "start", "dur"]) {
        const v = url.searchParams.get(k);
        if (v) qs.set(k, v);
      }
      const data = await req.arrayBuffer();
      if (!data.byteLength) return new Response(JSON.stringify({ error: "empty upload" }), { status: 400, headers: { ...cors, "Content-Type": "application/json" } });
      const r = await fetch(ENGINE_URL + "/convert?" + qs.toString(), {
        method: "POST",
        headers: { "X-DT-Key": ENGINE_KEY, "Content-Type": "application/octet-stream" },
        body: data,
      });
      if (!r.ok) {
        const t = await r.text();
        return new Response(t, { status: r.status, headers: { ...cors, "Content-Type": r.headers.get("content-type") ?? "application/json" } });
      }
      const headers: Record<string, string> = {
        ...cors,
        "Content-Type": r.headers.get("content-type") ?? "application/octet-stream",
        "Content-Disposition": r.headers.get("content-disposition") ?? "attachment",
      };
      const len = r.headers.get("content-length");
      if (len) headers["Content-Length"] = len;
      return new Response(r.body, { status: 200, headers });
    }

    // "vvc" / "vvcimg" (Visual Voice Creator, v109) are handled BEFORE
    // any JSON parsing, like "convert": the request body is the raw
    // uploaded file (the user's video for "vvc", one slideshow picture
    // for "vvcimg"); the options ride in the query string and are passed
    // straight to the engine. "vvc" answers { job: "<id>" } — the site
    // then polls the existing "genjob" action for the finished MP4.
    if (url.searchParams.get("action") === "vvc" || url.searchParams.get("action") === "vvcimg") {
      const act = url.searchParams.get("action") as string;
      const qs = new URLSearchParams();
      for (const k of ["voicestyle", "visualset", "token", "npics", "name"]) {
        const v = url.searchParams.get(k);
        if (v) qs.set(k, v);
      }
      const data = await req.arrayBuffer();
      if (!data.byteLength) return new Response(JSON.stringify({ error: "empty upload" }), { status: 400, headers: { ...cors, "Content-Type": "application/json" } });
      const r = await fetch(ENGINE_URL + "/" + act + "?" + qs.toString(), {
        method: "POST",
        headers: { "X-DT-Key": ENGINE_KEY, "Content-Type": "application/octet-stream" },
        body: data,
      });
      const text = await r.text();
      return new Response(text, {
        status: r.status,
        headers: { ...cors, "Content-Type": r.headers.get("content-type") ?? "application/json" },
      });
    }

    let body: Record<string, unknown> = {};
    if (req.method === "POST") { try { body = await req.json(); } catch { body = {}; } }
    const action = url.searchParams.get("action") || (body.action as string) || "";

    if (action === "resolve" || action === "subs") {
      const r = await fetch(engineFor(String(body.url ?? "")) + (action === "resolve" ? "/resolve" : "/subs"), {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-DT-Key": ENGINE_KEY },
        body: JSON.stringify(body),
      });
      const text = await r.text();
      return new Response(text, {
        status: r.status,
        headers: { ...cors, "Content-Type": r.headers.get("content-type") ?? "application/json" },
      });
    }

    if (action === "playlist") {
      const r = await fetch(engineFor(String(body.url ?? "")) + "/playlist", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-DT-Key": ENGINE_KEY },
        body: JSON.stringify({ url: body.url }),
      });
      const text = await r.text();
      return new Response(text, {
        status: r.status,
        headers: { ...cors, "Content-Type": r.headers.get("content-type") ?? "application/json" },
      });
    }

    if (action === "tts") {
      // AI Voiceover: text -> real speech MP3 from the engine. Binary
      // passthrough (same shape as "convert").
      const r = await fetch(ENGINE_URL + "/tts", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-DT-Key": ENGINE_KEY },
        body: JSON.stringify({ text: body.text ?? "", lang: body.lang ?? "en" }),
      });
      if (!r.ok) {
        const t = await r.text();
        return new Response(t, { status: r.status, headers: { ...cors, "Content-Type": r.headers.get("content-type") ?? "application/json" } });
      }
      const headers: Record<string, string> = {
        ...cors,
        "Content-Type": r.headers.get("content-type") ?? "audio/mpeg",
        "Content-Disposition": r.headers.get("content-disposition") ?? 'attachment; filename="dahot-voiceover.mp3"',
      };
      const len = r.headers.get("content-length");
      if (len) headers["Content-Length"] = len;
      return new Response(r.body, { status: 200, headers });
    }

    if (action === "genvideo" || action === "genmusic") {
      // AI Video / AI Music Generators: start a generation job on the
      // engine (the HF Space queue wait can exceed any single request
      // limit, so generation is a job — the site polls "genjob").
      // Returns { job: "<id>" } or the engine's error JSON as-is.
      const r = await fetch(ENGINE_URL + (action === "genvideo" ? "/genvideo" : "/genmusic"), {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-DT-Key": ENGINE_KEY },
        body: JSON.stringify(body),
      });
      const text = await r.text();
      return new Response(text, {
        status: r.status,
        headers: { ...cors, "Content-Type": r.headers.get("content-type") ?? "application/json" },
      });
    }

    if (action === "vvccap") {
      // Visual Voice Creator captions: the site sends the SRT it built
      // (transcript + chosen-language meaning, evenly timed) as JSON;
      // the engine stores it under the session token for the /vvc job.
      const r = await fetch(ENGINE_URL + "/vvccap", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-DT-Key": ENGINE_KEY },
        body: JSON.stringify(body),
      });
      const text = await r.text();
      return new Response(text, {
        status: r.status,
        headers: { ...cors, "Content-Type": r.headers.get("content-type") ?? "application/json" },
      });
    }

    if (action === "genjob") {
      // Poll a generator job: JSON {"state":"working"|"error",...}
      // while it runs; when done, the response IS the generated file
      // (binary passthrough, same shape as "convert").
      const job = url.searchParams.get("job") || (body.job as string) || "";
      if (!job) return new Response(JSON.stringify({ error: "missing job" }), { status: 400, headers: { ...cors, "Content-Type": "application/json" } });
      const r = await fetch(ENGINE_URL + "/genjob/" + encodeURIComponent(job), {
        headers: { "X-DT-Key": ENGINE_KEY },
      });
      const ct = r.headers.get("content-type") ?? "";
      if (!r.ok || ct.includes("json")) {
        const t = await r.text();
        return new Response(t, { status: r.status, headers: { ...cors, "Content-Type": ct || "application/json" } });
      }
      const headers: Record<string, string> = {
        ...cors,
        "Content-Type": ct || "application/octet-stream",
        "Content-Disposition": r.headers.get("content-disposition") ?? "attachment",
      };
      const len = r.headers.get("content-length");
      if (len) headers["Content-Length"] = len;
      return new Response(r.body, { status: 200, headers });
    }

    if (action === "psearch") {
      // Name search for the Search & Watch tabs (Facebook / TikTok /
      // Instagram), ordered by Rashid 2026-11-10. Route history:
      // HTML scraping (Bing/DDG/Mojeek/Brave) proved dead or degraded
      // from datacenter egress; Google's own Custom Search JSON API
      // is CLOSED to new customers since 2026-01-20 (live 403:
      // "This project does not have the access to Custom Search JSON
      // API"). Working route: Serper (google.serper.dev) — real
      // Google results as JSON, 2,500 free queries on signup, no
      // card. The per-platform site: query keeps results on video
      // pages; a post-filter drops anything that is not a video page.
      // 2026-11-11 fix: the Facebook query is domain-level only —
      // profile videos live at /<user>/videos/<slug>/<id>/, and
      // path-level site: prefixes (site:facebook.com/videos …) never
      // match those URLs, so the acceptance name search came back
      // empty until the filter below took over the selection.
      // Response: { results: [{title, url, thumb, snippet}] } — web
      // results carry no images, so thumbs are enriched afterwards
      // from each video's own cover (see fetchThumb below); a thumb
      // that cannot be fetched stays null and the site shows a
      // platform tile for that card.
      const SERPER_API_KEY = Deno.env.get("SERPER_API_KEY") ?? "";
      if (!SERPER_API_KEY) {
        return new Response(JSON.stringify({ error: "search_not_configured" }), { status: 500, headers: { ...cors, "Content-Type": "application/json" } });
      }
      const platform = String(body.platform ?? "");
      const q = String(body.q ?? "").slice(0, 120).trim();
      const SITE_QUERIES: Record<string, string> = {
        facebook: "site:facebook.com",
        tiktok: "site:tiktok.com",
        instagram: "site:instagram.com/reel OR site:instagram.com/p",
      };
      const siteQ = SITE_QUERIES[platform];
      if (!siteQ || !q) {
        return new Response(JSON.stringify({ error: "bad_search" }), { status: 400, headers: { ...cors, "Content-Type": "application/json" } });
      }
      // Daily caps (limits-protection step 3): count first; when a cap is
      // hit, answer with the site's "credits exhausted" shape so the tab
      // quietly returns to link-only mode instead of burning Serper credit.
      const psDayStr = psDay();
      const psKey = await psCallerKey(req);
      const psBefore = await psCounts(psDayStr, psKey);
      if (psBefore && (psBefore.user >= PS_USER_CAP || psBefore.global >= PS_GLOBAL_CAP)) {
        return new Response(JSON.stringify({ error: "search_failed", status: 429, detail: "daily_limit_reached" }), { status: 200, headers: { ...cors, "Content-Type": "application/json" } });
      }
      const r = await fetch("https://google.serper.dev/search", {
        method: "POST",
        headers: { "X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json" },
        body: JSON.stringify({ q: siteQ + " " + q, num: 10 }),
      });
      if (!r.ok) {
        const detail = (await r.text()).slice(0, 300);
        return new Response(JSON.stringify({ error: "search_failed", status: r.status, detail }), { status: 502, headers: { ...cors, "Content-Type": "application/json" } });
      }
      const data = await r.json();
      // The Serper search succeeded — count it against today's caps.
      if (psBefore) psBump(psDayStr, psKey, psBefore.user);
      const keep = (link: string): boolean => {
        try {
          const u = new URL(link);
          const h = u.hostname.toLowerCase();
          const p = u.pathname.toLowerCase();
          if (platform === "facebook") {
            if (h === "fb.watch") return true;
            if (!h.includes("facebook.com")) return false;
            if (p.includes("/watch/hashtag")) return false;
            if (p.includes("/reel/")) return true;
            if (p.startsWith("/watch")) return !!u.searchParams.get("v") || p.split("/").filter(Boolean).length >= 2;
            // Profile and Page video pages look like
            // /<name>/videos/<slug>/<numeric-id>/ — the /videos/ part
            // is NOT at the start of the path, which is exactly why
            // the query above stays domain-level.
            if (/\/videos\/.*\d{5,}/.test(p)) return true;
            return false;
          }
          if (platform === "tiktok") return h.includes("tiktok.com") && p.includes("/video/");
          if (platform === "instagram") return h.includes("instagram.com") &&
            (p.startsWith("/reel/") || p.startsWith("/reels/") || p.startsWith("/p/"));
        } catch { /* unparseable link */ }
        return false;
      };
      const seen = new Set<string>();
      const results: { title: string; url: string; thumb: string | null; snippet: string }[] = [];
      for (const it of (data.organic ?? [])) {
        const link = String(it?.link ?? "");
        if (!link || seen.has(link) || !keep(link)) continue;
        seen.add(link);
        const title = String(it?.title ?? "")
          .replace(/\s*[|–—-]\s*(Facebook|TikTok|Instagram)\s*$/i, "")
          .trim() || link;
        results.push({
          title: title.slice(0, 160),
          url: link,
          thumb: null,
          snippet: String(it?.snippet ?? "").replace(/\s+/g, " ").trim().slice(0, 140),
        });
        if (results.length >= 10) break;
      }
      // Thumbnail enrichment (Rashid, 2026-11-11: "thumbnail nahi nazr
      // aa raha, logo aa raha hai — set karo"). Serper results carry no
      // images, so each video's own cover is fetched afterwards:
      // Facebook and Instagram pages carry an og:image meta tag
      // readable with a browser user-agent (Facebook proven live from
      // this function's egress; Instagram answers with a login wall,
      // so its cards keep the tile for now). TikTok pages carry no
      // og:image and the tikwm API throttles THIS function's egress
      // after the first call (live test 2026-11-11: 1 of 6 covers,
      // parallel or sequential), so TikTok covers come from the
      // download engine's /thumbs endpoint, whose egress tikwm
      // accepts. Any failure leaves thumb null and the site keeps
      // showing the platform tile (its <img> also self-removes on
      // error), so enrichment can never break search.
      const BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36";
      const fetchThumb = async (link: string): Promise<string | null> => {
        try {
          const ctrl = new AbortController();
          const timer = setTimeout(() => ctrl.abort(), 6000);
          try {
            const pr = await fetch(link, { headers: { "User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9" }, signal: ctrl.signal });
            if (!pr.ok) return null;
            const html = (await pr.text()).slice(0, 400000);
            const m = html.match(/og:image"\s+content="([^"]+)"/) || html.match(/content="([^"]+)"\s+property="og:image"/);
            if (!m) return null;
            const img = m[1].replace(/&amp;/g, "&");
            return img.startsWith("http") ? img : null;
          } finally {
            clearTimeout(timer);
          }
        } catch { return null; }
      };
      if (platform === "tiktok") {
        try {
          const er = await fetch(ENGINE_URL + "/thumbs", {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-DT-Key": ENGINE_KEY },
            body: JSON.stringify({ urls: results.map((r) => r.url) }),
          });
          if (er.ok) {
            const ej = await er.json();
            const map = (ej && ej.thumbs) || {};
            for (const r of results) {
              const c = map[r.url];
              if (typeof c === "string" && c.startsWith("http")) r.thumb = c;
            }
          }
        } catch { /* covers unavailable — tiles stay */ }
      } else {
        await Promise.all(results.map(async (r) => { r.thumb = await fetchThumb(r.url); }));
      }
      return new Response(JSON.stringify({ results }), { status: 200, headers: { ...cors, "Content-Type": "application/json" } });
    }

    if (action === "fetch") {
      const u = url.searchParams.get("u") || (body.u as string) || "";
      const f = url.searchParams.get("f") || (body.f as string) || "";
      if (!u) return new Response(JSON.stringify({ error: "missing u" }), { status: 400, headers: { ...cors, "Content-Type": "application/json" } });
      const qs = new URLSearchParams({ u });
      if (f) qs.set("f", f);
      // Free-plan downloads carry the Dahot Tool brand watermark. Ask
      // for the CALLER's own status (their JWT, so the answer is about
      // them) and tell the engine to stamp only when the account is
      // positively Free. Any doubt — Premium, trial, lookup failure —
      // means NO stamp: never mark a paying user's file by mistake.
      try {
        const auth = req.headers.get("Authorization") ?? "";
        if (auth) {
          const st = await fetch((Deno.env.get("SUPABASE_URL") ?? "") + "/rest/v1/rpc/my_status", {
            method: "POST",
            headers: { "apikey": Deno.env.get("SUPABASE_ANON_KEY") ?? "", "Authorization": auth, "Content-Type": "application/json" },
            body: "{}",
          });
          if (st.ok) {
            const j = await st.json();
            // WATERMARK REQUESTS RE-ENABLED 2026-11-10 (engine fix
            // commit 5f30ebb): the engine now gates the stamp by a
            // duration x rate feasibility budget (DT_WM_BUDGET_S), so a
            // wm=1 fetch either stamps fast or returns the original
            // file immediately — it can no longer run past the host's
            // ~90s request limit. Proven on the live engines the same
            // day: Back4app TikTok 720p wm=1 -> 200 in 4.3s (gate
            // fallback, byte-identical file; the old code 504'd at
            // ~91s), Railway YouTube wm=1 -> 200 in 10.9s.
            if (j && j.signed_in === true && j.premium === false) qs.set("wm", "1");
          }
        }
      } catch { /* plan unknown -> no watermark */ }
      const r = await fetch(engineFor(u) + "/fetch?" + qs.toString(), { headers: { "X-DT-Key": ENGINE_KEY } });
      if (!r.ok) {
        const t = await r.text();
        return new Response(t, { status: r.status, headers: { ...cors, "Content-Type": "application/json" } });
      }
      const headers: Record<string, string> = {
        ...cors,
        "Content-Type": r.headers.get("content-type") ?? "video/mp4",
        "Content-Disposition": r.headers.get("content-disposition") ?? "attachment",
      };
      const len = r.headers.get("content-length");
      if (len) headers["Content-Length"] = len;
      return new Response(r.body, { status: 200, headers });
    }

    if (action === "webcheck") {
      // v110 website checkers (Ping, Is It Down, Server Status, Page
      // Snooper, Redirect Checker, Open Graph Checker, Meta Tags
      // Analyzer, sitemap discovery): the proxy fetches the target
      // page itself from Deno egress — browsers cannot fetch arbitrary
      // sites directly (CORS) — and answers structured facts.
      // body: { url, mode } — mode "status" | "redirects" | "source" | "meta".
      const mode = String(body.mode ?? "status");
      let target = String(body.url ?? "").trim();
      if (target && !/^https?:\/\//i.test(target)) target = "https://" + target;
      let u: URL;
      try { u = new URL(target); } catch { return new Response(JSON.stringify({ error: "bad_url" }), { status: 400, headers: { ...cors, "Content-Type": "application/json" } }); }
      if (u.protocol !== "http:" && u.protocol !== "https:") {
        return new Response(JSON.stringify({ error: "bad_url" }), { status: 400, headers: { ...cors, "Content-Type": "application/json" } });
      }
      const hostLc = u.hostname.toLowerCase();
      if (hostLc === "localhost" || hostLc.endsWith(".local") || /^(127\.|10\.|192\.168\.|169\.254\.|172\.(1[6-9]|2\d|3[01])\.)/.test(hostLc)) {
        return new Response(JSON.stringify({ error: "blocked_host" }), { status: 400, headers: { ...cors, "Content-Type": "application/json" } });
      }
      const ua = { "User-Agent": "Mozilla/5.0 (compatible; DahotToolChecker/1.0)" };
      const t0 = Date.now();
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 15000);
      try {
        if (mode === "redirects") {
          const chain: { url: string; status: number }[] = [];
          let cur = u.toString();
          for (let hop = 0; hop < 11; hop++) {
            const r = await fetch(cur, { redirect: "manual", signal: ctrl.signal, headers: ua });
            chain.push({ url: cur, status: r.status });
            const loc = r.headers.get("location");
            try { await r.body?.cancel(); } catch { /* noop */ }
            if (r.status >= 300 && r.status < 400 && loc) { cur = new URL(loc, cur).toString(); continue; }
            break;
          }
          clearTimeout(timer);
          return new Response(JSON.stringify({ ok: true, mode, chain, finalUrl: chain.length ? chain[chain.length - 1].url : u.toString(), finalStatus: chain.length ? chain[chain.length - 1].status : 0, ms: Date.now() - t0 }), { status: 200, headers: { ...cors, "Content-Type": "application/json" } });
        }
        const r = await fetch(u.toString(), { redirect: "follow", signal: ctrl.signal, headers: ua });
        const ms = Date.now() - t0;
        const base = { ok: true, mode, status: r.status, finalUrl: r.url, ms, contentType: r.headers.get("content-type") ?? "", server: r.headers.get("server") ?? "" };
        if (mode === "status") {
          try { await r.body?.cancel(); } catch { /* noop */ }
          clearTimeout(timer);
          return new Response(JSON.stringify(base), { status: 200, headers: { ...cors, "Content-Type": "application/json" } });
        }
        let html = await r.text();
        const sizeBytes = html.length;
        if (html.length > 700000) html = html.slice(0, 700000);
        clearTimeout(timer);
        if (mode === "source") {
          return new Response(JSON.stringify({ ...base, sizeBytes, html }), { status: 200, headers: { ...cors, "Content-Type": "application/json" } });
        }
        // mode "meta": pull the on-page SEO facts out of the HTML.
        const metas: Record<string, string> = {};
        for (const m of html.matchAll(/<meta\s+[^>]*>/gi)) {
          const tag = m[0];
          const km = tag.match(/(?:name|property)\s*=\s*["']([^"']+)["']/i);
          const vm = tag.match(/content\s*=\s*["']([^"']*)["']/i);
          const key = km ? km[1].toLowerCase() : "";
          if (key && !(key in metas)) metas[key] = (vm ? vm[1] : "").trim().slice(0, 500);
        }
        const titleM = html.match(/<title[^>]*>([^<]*)<\/title>/i);
        const canonM = html.match(/<link[^>]+rel\s*=\s*["']canonical["'][^>]*>/i);
        const canonH = canonM ? (canonM[0].match(/href\s*=\s*["']([^"']+)["']/i)?.[1] ?? "") : "";
        const h1s = [...html.matchAll(/<h1[^>]*>([\s\S]*?)<\/h1>/gi)].map((x) => x[1].replace(/<[^>]+>/g, "").trim());
        const imgs = [...html.matchAll(/<img\s+[^>]*>/gi)];
        const imgNoAlt = imgs.filter((x) => !/\salt\s*=\s*["'][^"']+["']/i.test(x[0])).length;
        const textOnly = html.replace(/<script[\s\S]*?<\/script>/gi, " ").replace(/<style[\s\S]*?<\/style>/gi, " ").replace(/<[^>]+>/g, " ");
        const meta = {
          title: titleM ? titleM[1].trim().slice(0, 300) : "",
          description: metas["description"] ?? "",
          keywords: metas["keywords"] ?? "",
          robotsMeta: metas["robots"] ?? "",
          canonical: canonH,
          viewport: "viewport" in metas,
          charset: /<meta[^>]+charset/i.test(html),
          og: { title: metas["og:title"] ?? "", description: metas["og:description"] ?? "", image: metas["og:image"] ?? "", type: metas["og:type"] ?? "", url: metas["og:url"] ?? "" },
          twitter: { card: metas["twitter:card"] ?? "", title: metas["twitter:title"] ?? "", description: metas["twitter:description"] ?? "", image: metas["twitter:image"] ?? "" },
          h1Count: h1s.length, h1First: (h1s[0] ?? "").slice(0, 200),
          h2Count: (html.match(/<h2[\s>]/gi) ?? []).length,
          imgCount: imgs.length, imgNoAlt,
          wordCount: textOnly.split(/\s+/).filter(Boolean).length,
        };
        return new Response(JSON.stringify({ ...base, sizeBytes, meta }), { status: 200, headers: { ...cors, "Content-Type": "application/json" } });
      } catch (e) {
        clearTimeout(timer);
        return new Response(JSON.stringify({ ok: false, error: "fetch_failed", detail: String(e).slice(0, 200), ms: Date.now() - t0 }), { status: 200, headers: { ...cors, "Content-Type": "application/json" } });
      }
    }

    if (action === "serp") {
      // v110 SEO checkers on Serper (Keyword Position Checker, Google
      // Index Checker, Indexed Pages Checker). Shares psearch's daily
      // caps, so the one-time 2,500 free grant is protected the same
      // way: per-caller 10/day, global 250/day (Asia/Karachi).
      const SERPER_API_KEY = Deno.env.get("SERPER_API_KEY") ?? "";
      if (!SERPER_API_KEY) {
        return new Response(JSON.stringify({ error: "search_not_configured" }), { status: 500, headers: { ...cors, "Content-Type": "application/json" } });
      }
      const q = String(body.q ?? "").slice(0, 200).trim();
      const num = Math.min(20, Math.max(1, Number(body.num ?? 10) || 10));
      if (!q) return new Response(JSON.stringify({ error: "bad_search" }), { status: 400, headers: { ...cors, "Content-Type": "application/json" } });
      const psDayStr = psDay();
      const psKey = await psCallerKey(req);
      const psBefore = await psCounts(psDayStr, psKey);
      if (psBefore && (psBefore.user >= PS_USER_CAP || psBefore.global >= PS_GLOBAL_CAP)) {
        return new Response(JSON.stringify({ error: "daily_limit_reached" }), { status: 200, headers: { ...cors, "Content-Type": "application/json" } });
      }
      const r = await fetch("https://google.serper.dev/search", {
        method: "POST",
        headers: { "X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json" },
        body: JSON.stringify({ q, num }),
      });
      if (!r.ok) {
        const detail = (await r.text()).slice(0, 300);
        return new Response(JSON.stringify({ error: "search_failed", status: r.status, detail }), { status: 502, headers: { ...cors, "Content-Type": "application/json" } });
      }
      const data = await r.json();
      if (psBefore) psBump(psDayStr, psKey, psBefore.user);
      const organic: { position: number; title: string; link: string }[] = [];
      let pos = 0;
      for (const it of (data.organic ?? [])) {
        pos++;
        organic.push({ position: Number(it?.position ?? pos), title: String(it?.title ?? ""), link: String(it?.link ?? "") });
      }
      return new Response(JSON.stringify({ organic }), { status: 200, headers: { ...cors, "Content-Type": "application/json" } });
    }

    if (action === "psi") {
      // v110 PageSpeed Insights proxy (Website SEO Score Checker,
      // Page Speed Test, Mobile Friendly Test). Keyless calls work
      // but are quota-tight; if a PAGESPEED_API_KEY secret is ever
      // set, it is used automatically.
      let target = String(body.url ?? "").trim();
      if (target && !/^https?:\/\//i.test(target)) target = "https://" + target;
      try { new URL(target); } catch { return new Response(JSON.stringify({ error: "bad_url" }), { status: 400, headers: { ...cors, "Content-Type": "application/json" } }); }
      const strategy = String(body.strategy ?? "mobile") === "desktop" ? "desktop" : "mobile";
      const key = Deno.env.get("PAGESPEED_API_KEY") ?? "";
      const api = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed?url=" + encodeURIComponent(target) +
        "&strategy=" + strategy + "&category=performance&category=accessibility&category=best-practices&category=seo" + (key ? "&key=" + encodeURIComponent(key) : "");
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 55000);
      try {
        const r = await fetch(api, { signal: ctrl.signal });
        clearTimeout(timer);
        if (!r.ok) {
          const detail = (await r.text()).slice(0, 300);
          return new Response(JSON.stringify({ error: "psi_failed", status: r.status, detail }), { status: 200, headers: { ...cors, "Content-Type": "application/json" } });
        }
        const data = await r.json();
        const cats = data?.lighthouseResult?.categories ?? {};
        const audits = data?.lighthouseResult?.audits ?? {};
        const num = (id: string) => { const a = audits[id]; return a && typeof a.numericValue === "number" ? Math.round(a.numericValue) : null; };
        const scores: Record<string, number | null> = {};
        for (const k of ["performance", "accessibility", "best-practices", "seo"]) {
          const s = cats[k]?.score;
          scores[k] = typeof s === "number" ? Math.round(s * 100) : null;
        }
        return new Response(JSON.stringify({
          ok: true, strategy, scores,
          metrics: {
            lcp: num("largest-contentful-paint"),
            cls: typeof audits["cumulative-layout-shift"]?.numericValue === "number" ? audits["cumulative-layout-shift"].numericValue : null,
            tbt: num("total-blocking-time"),
            fcp: num("first-contentful-paint"),
            si: num("speed-index"),
            inp: num("interaction-to-next-paint"),
          },
          finalUrl: data?.lighthouseResult?.finalDisplayedUrl ?? data?.id ?? target,
        }), { status: 200, headers: { ...cors, "Content-Type": "application/json" } });
      } catch (e) {
        clearTimeout(timer);
        return new Response(JSON.stringify({ error: "psi_failed", detail: String(e).slice(0, 200) }), { status: 200, headers: { ...cors, "Content-Type": "application/json" } });
      }
    }

    return new Response(JSON.stringify({ error: "unknown action" }), { status: 400, headers: { ...cors, "Content-Type": "application/json" } });
  } catch (e) {
    return new Response(JSON.stringify({ error: String(e) }), { status: 500, headers: { ...cors, "Content-Type": "application/json" } });
  }
});
