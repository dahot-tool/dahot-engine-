#!/usr/bin/env python3
"""Dahot Tool download engine.

A small HTTP service around yt-dlp:
  GET  /health                 -> {"ok": true}
  POST /resolve {"url": ...}   -> video info + downloadable format list
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


def ydl_opts(extra=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 2,
        "extractor_args": {
            # YouTube walls off the default web client from datacenter IPs;
            # these clients are the ones that still answer there.
            "youtube": {"player_client": ["android", "tv_embedded", "web_embedded"]},
        },
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


def safe_name(title: str) -> str:
    name = re.sub(r"[^\w\s\-\u0600-\u06FF]", "", title or "video").strip()
    return (name[:80] or "video")


@app.get("/health")
def health():
    return {"ok": True, "engine": "yt-dlp " + yt_dlp.version.__version__}


@app.post("/resolve")
def resolve(payload: dict, x_dt_key: str = Header(default="")):
    check_key(x_dt_key)
    url = (payload or {}).get("url", "").strip()
    if not url or not re.match(r"^https?://", url):
        raise HTTPException(status_code=400, detail="bad url")
    try:
        with yt_dlp.YoutubeDL(ydl_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
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
    return {
        "title": info.get("title"),
        "artist": info.get("uploader") or info.get("channel"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "webpage_url": info.get("webpage_url"),
        "formats": formats,
    }


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
    try:
        with yt_dlp.YoutubeDL(ydl_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
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
    return {"title": info.get("title"), "lang": want,
            "available": sorted(tracks), "text": text}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
