// ai-tools Edge Function — Dahot-Tool (hxqkgmmuaflghfwcqiez)
// Real-AI brain for the four writing tools (v39+): AI Script Writer,
// AI Caption + Hashtag Pro, AI Title + Description + Tags Pro and
// AI Hook Generator. Calls Google Gemini (free tier) with a per-tool
// system prompt. The site falls back to its built-in template output on
// ANY error from this function (quota, timeout, bad key), so the tools
// never fully break.
// Verify JWT: ON (the site calls it with the anon key, like the other
// functions). Secrets used: GEMINI_API_KEY (env only, never in code).
// Request:  POST { tool: "script"|"caption"|"title"|"hook",
//                  topic: string, platform?: string }
//           POST { tool: "paraphrase"|"grammarfix"|"translate",
//                  text: string, style?: string, lang?: string }
//                  (v84 writing tools: AI Paraphraser, AI Grammar Fixer,
//                  AI Translator — the user's own text, rewritten /
//                  corrected / translated per the PROMPTS entries)
//           POST { tool: "thumbdoctor", image: <base64>, mime?: string }
//                  (AI Thumbnail Doctor, v53: Gemini vision reviews an
//                  uploaded thumbnail image and scores it)
//           POST { tool: "diagram", text: string, style?: string }
//                  (Text to Diagram, v107: Gemini returns ONLY valid
//                  Mermaid diagram code for the chosen type — Flowchart,
//                  Mind Map, Timeline or Sequence Diagram; the site
//                  renders it with Mermaid and offers a PNG download)
// Response: { text, model } or { error }

import { serve } from "https://deno.land/std@0.224.0/http/server.ts";

const GEMINI_KEYS = [Deno.env.get("GEMINI_API_KEY") ?? "", Deno.env.get("GEMINI_API_KEY_2") ?? "", Deno.env.get("GEMINI_API_KEY_3") ?? "", Deno.env.get("GEMINI_API_KEY_4") ?? "", Deno.env.get("GEMINI_API_KEY_5") ?? ""].filter((k) => k.length > 0);
// Same probed model order as assistant-ai (2026-11-09): gemini-3.5-flash
// on v1beta is the fast primary; gemini-3.8-flash hangs and is only a
// last-resort fallback. Dead names just move the loop on.
const MODELS = ["gemini-3.5-flash", "gemini-3.8-flash"];

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

const COMMON = `Rules:
- Write in the SAME language and script as the topic the user gave (if the topic is in Roman Urdu, answer in Roman Urdu; if in English, answer in English; if in Urdu script, answer in Urdu script).
- Output ONLY the finished content the user asked for — no introduction, no "Here is your...", no explanation of what you did, no closing remarks.
- Be genuinely creative and specific to the topic. Never use placeholder text, never repeat the same line twice, never invent facts about real people or brands.`;

const PROMPTS: Record<string, string> = {
  script: `You are an expert short-video script writer for YouTube, TikTok, Instagram Reels and Facebook. The user gives a topic; write ONE complete, ready-to-read video script of about 45-60 seconds.
Structure it exactly like this:
🎬 HOOK (first 3 seconds): one gripping opening line.
📜 SCRIPT: the full spoken script in short natural lines, with a clear flow — hook, value/story, and a strong ending.
📣 CALL TO ACTION: one line asking viewers to like, comment, follow or subscribe.
${COMMON}`,

  caption: `You are an expert social media caption writer. The user gives a topic; write ONE engaging caption for it, then the hashtags.
Format exactly like this:
✍️ CAPTION:
(the caption — 2 to 4 short lines, lively, with 1-3 fitting emojis; write it so it can be copy-pasted directly)
#️⃣ HASHTAGS:
(15 to 20 relevant hashtags on ONE line, each starting with #, mixing popular and topic-specific ones)
${COMMON}`,

  title: `You are an expert YouTube SEO title and description writer. The user gives a video topic; produce title options, a full description and tags.
Format exactly like this:
🏆 BEST TITLE: (the single strongest click-worthy title, under 60 characters)
📌 MORE TITLE OPTIONS:
1. ...
2. ...
3. ...
4. ...
(4 more different options, each under 60 characters)
📝 DESCRIPTION:
(a ready-to-paste video description of 3-5 short paragraphs: a strong first 2 lines with the main keywords, what the viewer will learn/see, a call to action, and 3 hashtags at the end)
🏷️ TAGS:
(one line of 15-20 comma-separated search tags, most important first)
${COMMON}
- Titles must be honest clickbait: curiosity and benefit, no false claims, no ALL CAPS shouting.`,

  hook: `You are an expert at viral video hooks. The user gives a topic; write 10 different opening hooks for a short video on that topic.
Format: a numbered list 1. to 10., one hook per line, each under 15 words, each using a different angle (question, shocking fact, mistake warning, curiosity gap, challenge, story start, number promise, myth bust, before/after, direct call-out).
${COMMON}`,

  paraphrase: `You are an expert writing editor. The user gives a text and a style; rewrite the text in that style.
Styles: "Simple" = easy everyday words, short clear sentences; "Formal" = polite, professional, well-structured; "Shorter" = the same meaning in noticeably fewer words; "More detailed" = fuller, richer, with helpful detail added without inventing facts.
Rules:
- Keep the SAME language and script as the user's text (English stays English, Roman Urdu stays Roman Urdu, Urdu script stays Urdu script).
- Keep the meaning, facts and tone of the original — only the wording and the requested style change.
- Return ONLY the rewritten text — no introduction, no "Here is...", no explanation, no quotation marks around the whole text.`,

  grammarfix: `You are an expert proofreader. The user gives a text; correct its grammar, spelling and punctuation.
Rules:
- Keep the SAME language and script as the user's text (English stays English, Roman Urdu stays Roman Urdu, Urdu script stays Urdu script).
- Fix only what is wrong: grammar, spelling, punctuation, capitalisation and obvious word-choice mistakes. Do NOT rewrite, shorten, expand or change the meaning or the writer's own style.
- Return ONLY the corrected text — no introduction, no list of changes, no explanation, no quotation marks around the whole text.`,

  translate: `You are an expert translator. The user gives a text and a target language; translate the text into that target language.
Rules:
- Translate into the requested target language, in its standard everyday script (Urdu in Urdu script, Hindi in Devanagari, Punjabi in Gurmukhi, Sindhi in Sindhi script, Arabic in Arabic script, Chinese in Simplified Chinese characters).
- Translate the MEANING naturally, the way a native speaker would say it — not word by word. Keep names, numbers and formatting (line breaks, lists) as they are.
- Return ONLY the translation — no introduction, no "Here is the translation...", no explanation, no original text repeated.`,
};

const TRANSCRIBE_SYSTEM = `You are an expert transcriptionist. The user gives you ONE audio or video file. Write the EXACT transcript of everything spoken in it, word for word.
Rules:
- Write in the SAME language and script as the speech (English in English, Urdu in Urdu script, Roman Urdu stays Roman Urdu, Hindi in Devanagari, and so on). Never translate.
- If more than one person speaks, start each speaker's part on a new line with a label like "Speaker 1:" / "Speaker 2:".
- If there is no speech at all, reply with exactly: No speech found in this file.
- Output ONLY the transcript — no introduction, no summary, no comments.`;

const SCRIPTPROMPT_SYSTEM = `You are an expert video remake analyst. The user gives you ONE short video. Produce its full spoken script AND ready-to-use image prompts that could recreate its scenes.
Answer in EXACTLY this format:
🎬 SCRIPT:
(the complete spoken script, word for word, in the SAME language and script as the speech; if there is no speech, write: (No speech — visual/music only))
🎨 SCENE PROMPTS:
1. (a detailed English image-generation prompt for the first scene: subject, setting, clothing, lighting, colours, camera angle, style)
2. (the next scene's prompt, and so on — one prompt per clear scene or visual change, maximum 8)
Rules:
- The prompts must describe what is actually SEEN in the video — never invent people, places or objects that are not there.
- Write the prompts in English (image generators work best in English) even when the script is in another language.
- Output ONLY the two sections above — no introduction, no closing remarks.`;

const THUMBDOCTOR_SYSTEM = `You are an expert YouTube/TikTok thumbnail designer reviewing ONE uploaded video thumbnail image. Judge it the way a viewer scrolling a feed would: is it instantly readable, emotional and clickable at small size?
Answer in clear, simple English, in EXACTLY this format:
SCORE: (a number)/10
✅ WHAT WORKS:
- (1-3 short points, specific to THIS image)
🔧 FIX THESE:
- (3-5 concrete fixes, specific to THIS image: text size/words, contrast, face and expression, colours, clutter/background, focal point, curiosity gap)
Be honest: a weak thumbnail gets a low score. Never invent details you cannot see in the image. Output ONLY the review in the format above — no introduction, no closing remarks.`;

const SUMMARIZE_SYSTEM = `You are an expert video summariser. The user gives you the subtitle/transcript text of a video (it may be in any language). Write a clear summary of the VIDEO in the SAME language as the transcript.
Answer in EXACTLY this format:
📌 SUMMARY:
(5-8 short lines covering what the video is about, in order)
🔑 KEY POINTS:
- (3-6 short bullet points: the most important facts, steps or lessons)
Output ONLY the summary in the format above — no introduction, no closing remarks. If the given text is not a real transcript or is meaningless, say that in one line instead.`;

const IMGCAPTION_SYSTEM = `You are an expert social media caption writer. The user uploads ONE photo and may add a short note (platform and/or style). Write ready-to-post captions for THIS photo.
Answer in EXACTLY this format:
📸 CAPTION OPTIONS:
1. (first caption — lively, fitting THIS photo, 1-3 fitting emojis)
2. (second caption — a different angle/tone)
3. (third caption — short and punchy)
#️⃣ HASHTAGS:
(10 to 15 relevant hashtags on ONE line, each starting with #)
Rules:
- Write in the language of the user's note; if there is no note, write in English.
- Be specific to what is actually visible in the photo. Never invent people, brands, places or text you cannot see.
- Output ONLY the captions in the format above — no introduction, no closing remarks.`;

const DIAGRAM_SYSTEM = `You are an expert at turning text into clear diagrams. The user gives you a diagram type and a block of text (notes, steps or points, in any language). Convert the text into VALID Mermaid diagram code of exactly that type.
Rules:
- Output ONLY the Mermaid code — no markdown fences, no explanation, no introduction, nothing before or after the code.
- Flowchart: start with "flowchart TD". Put the steps in the order the text gives, connected with --> arrows. Write each step as a short label in square brackets, like A[First step]. Use a {curly brace} node only for a real yes/no decision.
- Mind Map: start with "mindmap". The second line is the main topic as the root, written as: root((Main topic)). Below it, add the main points as indented plain-text lines, and sub-points indented deeper under their point.
- Timeline: start with "timeline". The second line is "title " plus a short title. Then one line per event, in the order given, each written as: Step or year : what happened.
- Sequence Diagram: start with "sequenceDiagram". Declare the people or parts first with "participant" lines, then show their messages in order, like: A->>B: short message.
- Keep every label SHORT — a few words each. Use ONLY the facts in the user's text; never invent steps, people, dates or events.
- Write the labels in the SAME language and script as the user's text; the Mermaid keywords stay in English.
- Labels must not contain any of these characters: double quotes, semicolons, or the # sign. If the text contains them, rephrase the label without them.
- Keep the diagram focused: at most 15 steps, points or events. If the text is longer, merge minor points together.`;

// Best-effort per-IP soft limit (in-memory; resets with the isolate).
// The site already gates these tools behind sign-in + its daily counter;
// this only protects the free Gemini quota from direct abuse.
const hits = new Map<string, number[]>();
function rateLimited(ip: string): boolean {
  const now = Date.now();
  const arr = (hits.get(ip) ?? []).filter((t) => now - t < 10 * 60 * 1000);
  arr.push(now);
  hits.set(ip, arr);
  return arr.length > 40;
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...cors, "Content-Type": "application/json" },
  });
}

serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  if (req.method !== "POST") return json({ error: "method" }, 405);
  if (!GEMINI_KEYS.length) return json({ error: "no_key" }, 500);

  const ip = req.headers.get("x-forwarded-for")?.split(",")[0]?.trim() ?? "unknown";
  if (rateLimited(ip)) return json({ error: "rate_limited" }, 429);

  let body: Record<string, unknown> = {};
  try { body = await req.json(); } catch { return json({ error: "bad_json" }, 400); }

  const tool = String(body.tool ?? "");
  const system = tool === "thumbdoctor" ? THUMBDOCTOR_SYSTEM
    : tool === "diagram" ? DIAGRAM_SYSTEM
    : tool === "transcribe" ? TRANSCRIBE_SYSTEM
    : tool === "scriptprompt" ? SCRIPTPROMPT_SYSTEM
    : tool === "summarize" ? SUMMARIZE_SYSTEM
    : tool === "imgcaption" ? IMGCAPTION_SYSTEM
    : PROMPTS[tool];
  if (!system) return json({ error: "bad_tool" }, 400);

  let userParts: unknown[];
  if (tool === "thumbdoctor") {
    const image = String(body.image ?? "");
    const mime = String(body.mime ?? "image/jpeg").slice(0, 40);
    if (!image || image.length > 5_600_000) return json({ error: "bad_image" }, 400);
    userParts = [
      { text: "Review this video thumbnail." },
      { inline_data: { mime_type: mime, data: image } },
    ];
  } else if (tool === "transcribe" || tool === "scriptprompt") {
    const media = String(body.media ?? "");
    const mime = String(body.mime ?? (tool === "transcribe" ? "audio/mpeg" : "video/mp4")).slice(0, 60);
    if (!media || media.length > 5_600_000) return json({ error: "bad_media" }, 400);
    if (!/^(audio|video)\//.test(mime)) return json({ error: "bad_mime" }, 400);
    userParts = [
      { text: tool === "transcribe" ? "Transcribe this file." : "Analyse this video." },
      { inline_data: { mime_type: mime, data: media } },
    ];
  } else if (tool === "imgcaption") {
    const image = String(body.image ?? "");
    const mime = String(body.mime ?? "image/jpeg").slice(0, 40);
    if (!image || image.length > 5_600_000) return json({ error: "bad_image" }, 400);
    const note = String(body.topic ?? "").slice(0, 200).trim();
    userParts = [
      { text: note ? "Write captions for this photo. Note: " + note : "Write captions for this photo." },
      { inline_data: { mime_type: mime, data: image } },
    ];
  } else if (tool === "diagram") {
    // v107 Text to Diagram: the user's own text in body.text (like
    // summarize), plus the chosen diagram type in body.style.
    const text = String(body.text ?? "").slice(0, 6000).trim();
    if (text.length < 3) return json({ error: "empty" }, 400);
    const dtype = String(body.style ?? "").slice(0, 40).trim();
    const allowed = ["Flowchart", "Mind Map", "Timeline", "Sequence Diagram"];
    const type = allowed.includes(dtype) ? dtype : "Flowchart";
    userParts = [{ text: "Diagram type: " + type + "\n\nText:\n" + text }];
  } else if (tool === "summarize") {
    const transcript = String(body.text ?? "").slice(0, 12000).trim();
    if (transcript.length < 50) return json({ error: "empty" }, 400);
    userParts = [{ text: "Video transcript:\n" + transcript }];
  } else if (tool === "paraphrase" || tool === "grammarfix" || tool === "translate") {
    // v84 writing tools: the front-end sends the user's own text in
    // body.text (like summarize), plus body.style for paraphrase and
    // body.lang for translate.
    const text = String(body.text ?? "").slice(0, 6000).trim();
    if (!text) return json({ error: "empty" }, 400);
    let instruction = "";
    if (tool === "paraphrase") {
      const style = String(body.style ?? "").slice(0, 60).trim() || "Simple";
      instruction = "Style: " + style + "\n\nText:\n" + text;
    } else if (tool === "translate") {
      const lang = String(body.lang ?? "").slice(0, 60).trim() || "English";
      instruction = "Target language: " + lang + "\n\nText:\n" + text;
    } else {
      instruction = "Text:\n" + text;
    }
    userParts = [{ text: instruction }];
  } else {
    const topic = String(body.topic ?? "").slice(0, 500).trim();
    if (!topic) return json({ error: "empty" }, 400);
    const platform = String(body.platform ?? "").slice(0, 40).trim();
    const userText = platform
      ? `Topic: ${topic}\nPlatform: ${platform}`
      : `Topic: ${topic}`;
    userParts = [{ text: userText }];
  }

  const genCfg = tool === "diagram"
    ? { maxOutputTokens: 2000, temperature: 0.3, thinkingConfig: { thinkingBudget: 0 } }
    : tool === "transcribe"
    ? { maxOutputTokens: 4000, temperature: 0.2, thinkingConfig: { thinkingBudget: 0 } }
    : tool === "scriptprompt"
    ? { maxOutputTokens: 3000, temperature: 0.5, thinkingConfig: { thinkingBudget: 0 } }
    : { maxOutputTokens: 1200, temperature: 0.85, thinkingConfig: { thinkingBudget: 0 } };
  const payload = {
    system_instruction: { parts: [{ text: system }] },
    contents: [{ role: "user", parts: userParts }],
    generationConfig: genCfg,
  };

  // Candidate-first loop (same shape as assistant-ai, fixed 2026-11-17):
  // models x versions on the outside (v1 first — it answers or refuses
  // fast), API keys on the inside ONLY for 429 quota errors, a hard
  // per-attempt cap and an overall deadline. The old key-first loop
  // could grind for minutes in a Gemini 503 spike because one hanging
  // candidate was retried under every key.
  const isMedia = tool === "transcribe" || tool === "scriptprompt";
  const ATTEMPT_MS = isMedia ? 20_000 : 12_000;
  const DEADLINE = Date.now() + (isMedia ? 50_000 : 40_000);
  let lastStatus = 0;
  let lastDetail = "";
  let answered: Response | null = null;
  candidateLoop: for (const model of MODELS) {
    for (const version of ["v1", "v1beta"]) {
      const isPrimary = model === MODELS[0] && version === "v1";
      for (const apiKey of GEMINI_KEYS) {
        if (Date.now() > DEADLINE) break candidateLoop;
        let r: Response | null = null;
        const maxAttempts = isPrimary ? 3 : 1;
        for (let attempt = 0; attempt < maxAttempts; attempt++) {
          try {
            const ctrl = new AbortController();
            const timer = setTimeout(() => ctrl.abort(), ATTEMPT_MS);
            r = await fetch(
              `https://generativelanguage.googleapis.com/${version}/models/${model}:generateContent`,
              {
                method: "POST",
                signal: ctrl.signal,
                headers: { "Content-Type": "application/json", "x-goog-api-key": apiKey },
                body: JSON.stringify(payload),
              },
            );
            clearTimeout(timer);
          } catch (e) {
            lastStatus = 0;
            lastDetail = "network: " + String(e).slice(0, 200);
            r = null;
            break;
          }
          if (r.status !== 503 || attempt === maxAttempts - 1) break;
          await new Promise((res) => setTimeout(res, attempt === 0 ? 1500 : 3000));
        }
        if (!r) break;
        lastStatus = r.status;
        if (r.status === 429) continue;
        if (!r.ok) {
          lastDetail = (await r.text()).slice(0, 300);
          break;
        }
        const data = await r.json();
        const text = (data?.candidates?.[0]?.content?.parts ?? [])
          .map((p: { text?: string }) => p?.text ?? "")
          .join("")
          .trim();
        if (!text) {
          lastDetail = "empty answer from " + model;
          break;
        }
        answered = json({ text: text.slice(0, isMedia ? 12000 : 6000), model });
        break candidateLoop;
      }
    }
  }
  if (answered) return answered;
  if (lastStatus === 429) return json({ error: "quota" }, 429);
  return json({ error: "no_model", status: lastStatus, detail: lastDetail }, 502);
});
