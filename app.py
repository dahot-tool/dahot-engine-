#!/usr/bin/env python3
"""Dahot Tool download engine.

A small HTTP service around yt-dlp:
  GET  /health                 -> {"ok": true}
  POST /resolve {"url": ...}   -> video info + downloadable format list
                                  (playlist URLs return the playlist shape)
  POST /playlist {"url": ...}  -> playlist listing: entries with ready-to-
                                  fetch watch URLs (feed each entry URL to
                                  /fetch to download it)
  GET  /fetch?u=<url>&f=<fmt>  -> streams the file through this server with
                                  Content-Disposition: attachment, so the
                                  browser saves it into the device Downloads
                                  folder (CDN links are IP-bound, so handing
                                  them to the browser directly would fail).

Access is gated by the shared header X-DT-Key (env DT_ENGINE_KEY) so the free
hosting quota cannot be abused by strangers. Plan limits (Free/Premium,
quality caps, watermark rules) are enforced by the website before it calls
this engine; the engine itself only extracts and delivers.
"""
import base64
import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

import yt_dlp

ENGINE_KEY = os.environ.get("DT_ENGINE_KEY", "")
MAX_HEIGHT_FREE = 720  # the site passes plan info; kept simple for now

COOKIES_PATH = "/tmp/yt-cookies.txt"


def _load_cookies():
    """Decode the YT_COOKIES_B64 env var (base64 of a Netscape cookies.txt
    from a spare account) into a local file for yt-dlp. YouTube, Reddit
    and Vimeo wall off datacenter IPs unless the request carries a signed-
    in session; the cookies are domain-scoped, so other platforms are
    unaffected. Absent or invalid value -> no cookiefile at all, and the
    engine keeps serving every platform exactly as before. The cookie
    material itself is never printed or logged."""
    raw = os.environ.get("YT_COOKIES_B64", "").strip()
    if not raw:
        # Back4app's .env upload skips very long lines, so the value may
        # instead arrive split across numbered part variables
        # (YT_COOKIES_P01, YT_COOKIES_P02, ...); join them in order.
        parts = []
        for i in range(1, 65):
            part = os.environ.get(f"YT_COOKIES_P{i:02d}")
            if part is None:
                break
            parts.append(part.strip())
        raw = "".join(parts)
    if not raw:
        return None
    try:
        data = base64.b64decode(raw, validate=True)
        if not data or b"youtube" not in data.lower():
            raise ValueError("decoded content is not a YouTube cookies file")
        with open(COOKIES_PATH, "wb") as fh:
            fh.write(data)
        os.chmod(COOKIES_PATH, 0o600)
        print("[cookies] cookiefile loaded for yt-dlp", flush=True)
        return COOKIES_PATH
    except Exception as e:
        print("[cookies] WARNING: YT_COOKIES_B64 unusable "
              f"({type(e).__name__}); continuing without cookies",
              flush=True)
        return None


COOKIEFILE = _load_cookies()

app = FastAPI(title="Dahot Tool Engine")


@app.exception_handler(Exception)
async def unhandled(request, exc):
    # Never fail silently: return the real error to the (key-holding) caller
    # and print it, so a broken host environment is diagnosable without logs.
    import traceback
    traceback.print_exc()
    return JSONResponse(status_code=500, content={
        "ok": False,
        "error": type(exc).__name__ + ": " + str(exc)[:300],
        "trace": traceback.format_exc()[-1500:],
    })


def check_key(x_dt_key: str = Header(default="")):
    if ENGINE_KEY and x_dt_key != ENGINE_KEY:
        raise HTTPException(status_code=403, detail="forbidden")


def ydl_opts(extra=None, player_client="KEEP"):
    """Build yt-dlp options. player_client controls the YouTube extractor's
    player_client argument:
      "KEEP" (default) -> the long-standing combo below; every non-YouTube
                          path and /fetch use exactly this, unchanged.
      None             -> yt-dlp's built-in default client set (no override).
      [names]          -> exactly those clients, in order.
    """
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 2,
    }
    if COOKIEFILE:
        # Signed-in session for the platforms that wall off datacenter
        # IPs (YouTube/Reddit/Vimeo). Domain-scoped: no effect elsewhere.
        opts["cookiefile"] = COOKIEFILE
    if player_client == "KEEP":
        opts["extractor_args"] = {
            # YouTube walls off the default web client from datacenter IPs;
            # these clients are the ones that still answer there.
            "youtube": {"player_client": ["android", "tv_embedded", "web_embedded"]},
        }
    elif player_client is not None:
        opts["extractor_args"] = {
            "youtube": {"player_client": list(player_client)},
        }
    try:
        import curl_cffi  # noqa: F401  (impersonation backend; must be installed)
        from yt_dlp.networking.impersonate import ImpersonateTarget
        opts["impersonate"] = ImpersonateTarget("chrome")
    except Exception:
        pass
    if extra:
        opts.update(extra)
    return opts


YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")


def is_youtube_url(url: str) -> bool:
    try:
        host = urllib.parse.urlparse(url).netloc.lower().split(":")[0]
    except Exception:
        return False
    return any(host == h or host.endswith("." + h) for h in YOUTUBE_HOSTS)


# Player-client strategies tried in order for YouTube URLs: the first
# strategy whose extraction succeeds
# wins; a bot-wall error ("Sign in to confirm you're not a bot") or any
# other extraction failure falls through to the next strategy. The final
# entry is the long-standing combo, so YouTube behaviour can only improve
# on the old single path, never regress below it.
YT_CLIENT_STRATEGIES = [
    None,  # yt-dlp built-in default
    ["tv"],
    ["ios"],
    ["android"],
    ["web_safari"],
    ["mweb"],
    ["android", "tv_embedded", "web_embedded"],
]


def _client_label(player_client) -> str:
    return "default" if player_client is None else "+".join(player_client)


def cycle_youtube(fn):
    """Run fn(player_client) across YT_CLIENT_STRATEGIES; first success
    wins. Returns (result, client_label). Re-raises the last DownloadError
    when every strategy fails."""
    last_err = None
    for pc in YT_CLIENT_STRATEGIES:
        try:
            result = fn(pc)
            print(f"[yt-cycle] OK via {_client_label(pc)}", flush=True)
            return result, _client_label(pc)
        except yt_dlp.utils.DownloadError as e:
            last_err = e
            print(f"[yt-cycle] failed via {_client_label(pc)}: {str(e)[:140]}",
                  flush=True)
    raise last_err


def extract_once(url, extra=None, player_client="KEEP"):
    with yt_dlp.YoutubeDL(ydl_opts(extra, player_client=player_client)) as ydl:
        return ydl.extract_info(url, download=False)


def safe_name(title: str) -> str:
    name = re.sub(r"[^\w\s\-\u0600-\u06FF]", "", title or "video").strip()
    return (name[:80] or "video")


@app.get("/health")
def health():
    return {"ok": True, "engine": "yt-dlp " + yt_dlp.version.__version__}


def extract_playlist(url: str, player_client="KEEP") -> dict:
    """Flat playlist extraction: list up to 50 entries without resolving
    each video, so playlist/collection URLs return something useful.
    Each entry's url is the watch URL /fetch can download directly."""
    opts = ydl_opts({"extract_flat": "in_playlist", "playlistend": 50},
                    player_client=player_client)
    opts.pop("noplaylist", None)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = []
    for e in info.get("entries") or []:
        if not e:
            continue
        thumb = e.get("thumbnail")
        if not thumb and e.get("thumbnails"):
            thumb = e["thumbnails"][-1].get("url")
        entries.append({
            "id": e.get("id"),
            "title": e.get("title"),
            "url": e.get("url") or e.get("webpage_url"),
            "duration": e.get("duration"),
            "thumbnail": thumb,
        })
    return {
        "is_playlist": True,
        "title": info.get("title"),
        "count": len(entries),
        "entries": entries,
    }


@app.post("/playlist")
def playlist(payload: dict, x_dt_key: str = Header(default="")):
    check_key(x_dt_key)
    url = (payload or {}).get("url", "").strip()
    if not url or not re.match(r"^https?://", url):
        raise HTTPException(status_code=400, detail="bad url")
    try:
        if is_youtube_url(url):
            result, label = cycle_youtube(
                lambda pc: extract_playlist(url, player_client=pc))
            result["yt_client"] = label
            return result
        return extract_playlist(url)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=422, detail=str(e)[:300])


@app.post("/resolve")
def resolve(payload: dict, x_dt_key: str = Header(default="")):
    check_key(x_dt_key)
    url = (payload or {}).get("url", "").strip()
    if not url or not re.match(r"^https?://", url):
        raise HTTPException(status_code=400, detail="bad url")
    yt_client = None
    try:
        if is_youtube_url(url):
            info, yt_client = cycle_youtube(
                lambda pc: extract_once(url, player_client=pc))
        else:
            info = extract_once(url)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=422, detail=str(e)[:300])
    if info.get("_type") == "playlist" or "entries" in info:
        # Playlist URL: the single-video path yields zero formats, so
        # answer with the playlist listing instead.
        try:
            if is_youtube_url(url):
                result, label = cycle_youtube(
                    lambda pc: extract_playlist(url, player_client=pc))
                result["yt_client"] = label
                return result
            return extract_playlist(url)
        except yt_dlp.utils.DownloadError as e:
            raise HTTPException(status_code=422, detail=str(e)[:300])
    formats = []
    for f in info.get("formats", []):
        if f.get("vcodec") in (None, "none"):
            continue
        w = f.get("width")
        h = f.get("height")
        # Quality tier ("720p") is the SMALLER dimension: for portrait videos
        # yt-dlp's height is the long side, so use min(width, height).
        tier = min(w, h) if (w and h and w > 0 and h > 0) else h
        formats.append({
            "id": f.get("format_id"),
            "height": tier,
            "width": w,
            "ext": f.get("ext"),
            "size": f.get("filesize") or f.get("filesize_approx"),
            "has_audio": f.get("acodec") not in (None, "none"),
        })
    formats.sort(key=lambda x: (x["height"] or 0))
    result = {
        "title": info.get("title"),
        "artist": info.get("uploader") or info.get("channel"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "webpage_url": info.get("webpage_url"),
        "formats": formats,
    }
    if yt_client is not None:
        # Which player_client strategy got past the YouTube bot wall.
        result["yt_client"] = yt_client
    return result


@app.get("/fetch")
def fetch(u: str, f: str = "", x_dt_key: str = Header(default="")):
    check_key(x_dt_key)
    if not re.match(r"^https?://", u or ""):
        raise HTTPException(status_code=400, detail="bad url")
    fmt = f or "bv*[height<=720]+ba/b[height<=720]/b"
    tmpdir = "/tmp/dt-fetch"
    os.makedirs(tmpdir, exist_ok=True)
    opts = ydl_opts({
        "format": fmt,
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
        "max_filesize": 900 * 1024 * 1024,
    })
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(u, download=True)
            path = ydl.prepare_filename(info)
            if not os.path.exists(path):  # merged file may switch extension
                base = os.path.splitext(path)[0]
                for cand in (base + ".mp4", base + ".mkv", base + ".webm"):
                    if os.path.exists(cand):
                        path = cand
                        break
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=422, detail=str(e)[:300])
    if not os.path.exists(path):
        raise HTTPException(status_code=500, detail="file missing after download")

    filename = safe_name(info.get("title", "video")) + os.path.splitext(path)[1]
    size = os.path.getsize(path)

    def stream():
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1024 * 256)
                if not chunk:
                    break
                yield chunk
        try:
            os.remove(path)
        except OSError:
            pass

    return StreamingResponse(
        stream(),
        media_type="video/mp4",
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''"
                                   + urllib.parse.quote(filename),
            "Content-Length": str(size),
        },
    )


@app.post("/subs")
def subs(payload: dict, x_dt_key: str = Header(default="")):
    """Subtitle / transcript extraction for the Subtitle Downloader and
    YouTube Transcript tools. Returns the plain text of the best available
    subtitle track (manual subtitles first, then automatic captions)."""
    check_key(x_dt_key)
    url = (payload or {}).get("url", "").strip()
    lang = (payload or {}).get("lang", "").strip()
    if not url or not re.match(r"^https?://", url):
        raise HTTPException(status_code=400, detail="bad url")
    import io
    import urllib.request as ureq
    yt_client = None
    try:
        if is_youtube_url(url):
            info, yt_client = cycle_youtube(
                lambda pc: extract_once(url, player_client=pc))
        else:
            info = extract_once(url)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=422, detail=str(e)[:300])
    tracks = {}
    for source in ("subtitles", "automatic_captions"):
        for code, fmt_list in (info.get(source) or {}).items():
            tracks.setdefault(code, fmt_list)
    if not tracks:
        raise HTTPException(status_code=404, detail="no subtitles available")
    want = lang or ("en" if "en" in tracks else sorted(tracks)[0])
    if want not in tracks:
        want = sorted(tracks)[0]
    fmt = next((f for f in tracks[want] if f.get("ext") == "json3"), None) \
        or next((f for f in tracks[want] if f.get("ext") == "srv3"), None) \
        or tracks[want][0]
    try:
        req = ureq.Request(fmt["url"], headers={"User-Agent": "Mozilla/5.0"})
        raw = ureq.urlopen(req, timeout=30).read().decode("utf-8", "replace")
    except Exception as e:
        raise HTTPException(status_code=502, detail="subtitle fetch failed")
    text = ""
    if fmt.get("ext") in ("json3", "srv3") or raw.lstrip().startswith("{"):
        import json as _json
        try:
            data = _json.loads(raw)
            parts = []
            for ev in data.get("events", []):
                line = "".join(s.get("utf8", "") for s in ev.get("segs", []))
                if line.strip():
                    parts.append(line.strip())
            text = "\n".join(parts)
        except Exception:
            text = ""
    if not text:
        # VTT/SRT style fallback: keep caption lines, drop timing and tags.
        lines = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or "-->" in line or line.startswith(("WEBVTT", "Kind:", "Language:")):
                continue
            line = re.sub(r"<[^>]+>", "", line)
            if line and (not lines or lines[-1] != line):
                lines.append(line)
        text = "\n".join(lines)
    result = {"title": info.get("title"), "lang": want,
              "available": sorted(tracks), "text": text}
    if yt_client is not None:
        result["yt_client"] = yt_client
    return result


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
