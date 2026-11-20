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
  POST /genvideo {"prompt":...} / POST /genmusic {"prompt":..., "duration":s}
                               -> {"job": id}: AI generation on public HF
                                  Spaces; poll GET /genjob/<id> -> working /
                                  error JSON, or the finished file itself.
  POST /editimage {"image_b64":..., "prompt":...}
                               -> {"job": id}: AI photo edit on a public HF
                                  Space; poll GET /genjob/<id> the same way.
  POST /vvcimg?token=<hex>     -> raw-body picture upload for a Visual
                                  Voice Creator session (max 4 pictures).
  POST /vvccap {"token":..., "srt":...}
                               -> stores the session's captions SRT.
  POST /vvc?voicestyle=&visualset=&token=&npics=
                               -> {"job": id}: Visual Voice Creator —
                                  the caller's video (raw body) keeps only
                                  its own enhanced voice; new slideshow
                                  visuals are rendered with ffmpeg; poll
                                  GET /genjob/<id> for the finished MP4.

Access is gated by the shared header X-DT-Key (env DT_ENGINE_KEY) so the free
hosting quota cannot be abused by strangers. Plan limits (Free/Premium,
quality caps, watermark rules) are enforced by the website before it calls
this engine; the engine itself only extracts and delivers.
"""
import base64
import os
import re
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

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

# PO-token provider (bgutil-ytdlp-pot-provider 2.0.2, plugin + server in
# lockstep). PRODUCTION VERDICT 2026-11-09: the sidecar ran inside this
# container, tokens were minted and presented (container logs prove it),
# and YouTube STILL returned storyboard-only formats for every client —
# the wall is this datacenter egress, not the token. The provider is
# therefore DISABLED by default (it also idles at ~200 MB of the free
# container's 256 MB). Set POT_PROVIDER_URL to a provider URL to retry
# this route in the future; "" (the default) is the pre-POT behaviour.
POT_PROVIDER_URL = os.environ.get(
    "POT_PROVIDER_URL", "").strip()
print(f"[pot] PO-token provider: {POT_PROVIDER_URL or 'disabled'}",
      flush=True)

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


def ydl_opts(extra=None, player_client="KEEP", impersonate=True):
    """Build yt-dlp options. player_client controls the YouTube extractor's
    player_client argument:
      "KEEP" (default) -> the long-standing combo below; every non-YouTube
                          path and /fetch use exactly this, unchanged.
      None             -> yt-dlp's built-in default client set (no override).
      [names]          -> exactly those clients, in order.
    impersonate=False drops the Chrome TLS impersonation (some YouTube
    clients answer with storyboard-only formats when the request claims
    to be a TV/Android app but arrives with a Chrome fingerprint).
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
    if POT_PROVIDER_URL:
        # bgutil PO-token provider, merged AFTER the player_client
        # branches so every strategy in YT_CLIENT_STRATEGIES gets it
        # (including the default, None, branch, which sets no
        # extractor_args of its own). The plugin pings base_url itself
        # and bypasses proxies for provider calls; when the provider is
        # unreachable it simply reports itself unavailable and yt-dlp
        # proceeds without a token, as before.
        opts.setdefault("extractor_args", {})["youtubepot-bgutilhttp"] = {
            "base_url": [POT_PROVIDER_URL],
        }
    if impersonate:
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


# ---------------------------------------------------------------------------
# TeraBox: yt-dlp has no extractor for it, so shares are resolved through
# TeraBox's own sharing API with the spare account's session. Only the
# `ndus` cookie is ever sent (env TERABOX_NDUS): the combined cookie jar
# belongs to yt-dlp, and TeraBox's download hosts reject a fetch carrying
# anything more than ndus. The flow is shorturlinfo for the file list
# (its own dlink field comes back empty) followed by sharedownload,
# which exchanges the share's sign/timestamp + the file ids for the real
# dlinks. dlinks are short-lived, so /fetch re-resolves the share at
# download time and never stores or hands out a dlink. The dlink GET
# itself goes through _tb_download: the download hosts refuse plain
# urllib's TLS fingerprint, so it uses curl_cffi's Chrome
# impersonation (urllib as fallback) with the ndus-only cookie.
# A share resolved on a mirror host gets its dlinks from that mirror's
# download host, which can refuse even the impersonated GET while the
# main host would serve the same file, so /fetch retries exactly once
# when a mirror-issued dlink is refused with HTTP 403: it reuses the
# sign context captured during the natural resolution and asks only
# www.terabox.com's sharedownload for a main-host dlink (the main
# host's shorturlinfo lookup itself is verification-walled from some
# egresses, so re-running it there is not an option — see
# fetch_terabox). The retry is a silent detour: if any of its steps
# fail, the original refusal is what surfaces.
TERABOX_NDUS = os.environ.get("TERABOX_NDUS", "").strip()
TERABOX_HOSTS = ("terabox.com", "teraboxapp.com", "1024terabox.com",
                 "1024tera.com", "terabox.app", "mirrobox.com",
                 "freemb.com", "gibibox.com", "momerybox.com",
                 "terasharelink.com", "teraboxlink.com", "nephobox.com",
                 "4funbox.com", "freeterabox.com", "tibibox.com",
                 "terafileshare.com", "teraboxshare.com")
TB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
         "AppleWebKit/537.36 (KHTML, like Gecko) "
         "Chrome/126.0.0.0 Safari/537.36")
TB_VIDEO_EXTS = {"mp4", "mkv", "avi", "mov", "webm", "m4v", "ts",
                 "flv", "wmv", "mpg", "mpeg", "3gp"}


def is_terabox_url(url: str) -> bool:
    try:
        host = urllib.parse.urlparse(url).netloc.lower().split(":")[0]
    except Exception:
        return False
    return any(host == h or host.endswith("." + h) for h in TERABOX_HOSTS)


def _tb_shorturl_id(url: str):
    """Share id from a /s/<id> path or a ?surl= / ?shorturl= query."""
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return None
    m = re.search(r"/s/([A-Za-z0-9_\-]+)", parsed.path or "")
    if m:
        return m.group(1)
    qs = urllib.parse.parse_qs(parsed.query or "")
    for key in ("surl", "shorturl"):
        if qs.get(key):
            return qs[key][0]
    return None


def _tb_api_host(url: str) -> str:
    """API calls go to the share's own host: dlinks are bound to the
    issuing domain, so a mirror link must be resolved on that mirror."""
    try:
        host = urllib.parse.urlparse(url).netloc.lower().split(":")[0]
    except Exception:
        host = ""
    if any(host == h or host.endswith("." + h) for h in TERABOX_HOSTS):
        return host
    return "www.terabox.com"


class _TbRedirect(urllib.request.HTTPRedirectHandler):
    """Keep the session ndus-only on every redirect hop (dlink hosts):
    the original request's headers (User-Agent, Referer) carry over,
    and the Cookie is re-attached as ndus and nothing else."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        keep = {}
        for k, v in req.headers.items():
            if k.lower() in ("cookie", "host", "content-length",
                             "content-type"):
                continue
            keep[k] = v
        keep["Cookie"] = "ndus=" + TERABOX_NDUS
        if not any(k.lower() == "user-agent" for k in keep):
            keep["User-Agent"] = TB_UA
        return urllib.request.Request(
            newurl,
            headers=keep,
            origin_req_host=req.origin_req_host,
            unverifiable=True,
        )


def _tb_open(req):
    opener = urllib.request.build_opener(_TbRedirect())
    return opener.open(req, timeout=60)


def _tb_get_json(api_url: str) -> dict:
    import json as _json
    req = urllib.request.Request(api_url, headers={
        "User-Agent": TB_UA,
        "Cookie": "ndus=" + TERABOX_NDUS,
        "Referer": "https://www.terabox.com/",
        "Accept": "application/json, text/plain, */*",
    })
    with _tb_open(req) as resp:
        return _json.loads(resp.read().decode("utf-8", "replace"))


def _tb_shorturlinfo(host: str, surl: str) -> dict:
    def call(sid):
        api = ("https://" + host + "/api/shorturlinfo?" +
               urllib.parse.urlencode({"shorturl": sid, "root": 1}))
        return _tb_get_json(api)

    data = call(surl)
    if data.get("errno") != 0 and surl.startswith("1") and len(surl) > 8:
        # Some issuers register the share without its leading "1".
        retry = call(surl[1:])
        if retry.get("errno") == 0:
            return retry
    return data


def _tb_post_json(api_url: str, form: dict) -> dict:
    import json as _json
    req = urllib.request.Request(
        api_url, data=urllib.parse.urlencode(form).encode(), headers={
            "User-Agent": TB_UA,
            "Cookie": "ndus=" + TERABOX_NDUS,
            "Referer": "https://www.terabox.com/",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded",
        })
    with _tb_open(req) as resp:
        return _json.loads(resp.read().decode("utf-8", "replace"))


def _tb_sharedownload(host: str, info: dict, fs_ids: list) -> dict:
    """Exchange the share's sign/timestamp + file ids for real dlinks.
    Returns {fs_id: dlink}; empty when TeraBox refuses."""
    import json as _json
    ids = []
    for i in fs_ids:
        try:
            ids.append(int(i))
        except (TypeError, ValueError):
            continue
    if not ids or not info.get("sign"):
        return {}
    query = urllib.parse.urlencode({
        "app_id": 250528, "channel": "chunlei", "clienttype": 12,
        "web": 1, "sign": info.get("sign") or "",
        "timestamp": info.get("timestamp") or "",
    })
    form = {
        "encrypt": 0,
        "extra": _json.dumps({"sekey": info.get("randsk") or ""}),
        "fid_list": _json.dumps(ids),
        "primaryid": info.get("shareid"),
        "uk": info.get("uk"),
        "product": "share",
        "type": "nolimit",
    }
    data = _tb_post_json("https://" + host + "/api/sharedownload?" + query,
                         form)
    if data.get("errno") != 0:
        return {}
    return {str(it.get("fs_id")): it.get("dlink")
            for it in data.get("list") or [] if it.get("dlink")}


def _tb_list_dir(host: str, surl: str, dir_path: str) -> list:
    params = {"order": "name", "desc": 0, "shorturl": surl,
              "dir": dir_path, "root": 0, "page": 1, "num": 100, "web": 1}
    data = _tb_get_json("https://" + host + "/api/list?" +
                        urllib.parse.urlencode(params))
    if data.get("errno") != 0:
        return []
    return data.get("list") or []


# TeraBox rate-limits share lookups per egress IP (observed locally:
# after a burst of calls, shorturlinfo answers errno 400141 to every
# identity until the window refills). The site's normal flow resolves a
# share and fetches from it seconds later, so a resolution is reused
# for 120 s — the dlink stays fresh (they live far longer), and a
# resolve+fetch pair costs TeraBox one lookup sequence, not two.
_TB_CACHE = {}
_TB_INFO = {}
TB_CACHE_TTL = 120


def _tb_cache_key(url: str, force_host: str = None) -> str:
    try:
        p = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(p.query)
        qs.pop("dtfid", None)
        key = urllib.parse.urlunparse(
            p._replace(query=urllib.parse.urlencode(qs, doseq=True)))
    except Exception:
        key = url
    if force_host:
        # A forced-host resolution is answered by a different API host
        # and yields dlinks issued by that host's download servers, so
        # it must never share a cache slot with the natural resolution
        # of the same share.
        key += "|tbhost=" + force_host
    return key


def _tb_sign_context(url: str):
    """The sign context captured when this share was last resolved
    naturally — sign/timestamp/shareid/uk/randsk from its shorturlinfo
    response — or None. /fetch's main-host retry reuses it to ask
    www.terabox.com's sharedownload for a main-host dlink without
    re-running the lookup on the main host (see _tb_collect_files,
    where the context is stored beside the cache entry)."""
    return _TB_INFO.get(_tb_cache_key(url))


def _tb_collect_files(url: str, force_host: str = None):
    """Resolve a TeraBox share URL to its downloadable file entries.
    Raises HTTPException with a clean detail on any TeraBox-side refusal
    (bad link, verification demand, empty dlink) instead of crashing.

    With force_host set, every API call goes to that host instead of
    the share's own: the share id is host-independent, and resolving
    through www.terabox.com yields dlinks from the main download host
    rather than a mirror's."""
    if not TERABOX_NDUS:
        raise HTTPException(status_code=422,
                            detail="terabox: sign-in cookie not configured")
    key = _tb_cache_key(url, force_host)
    hit = _TB_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < TB_CACHE_TTL:
        return [dict(f) for f in hit[1]]
    surl = _tb_shorturl_id(url)
    if not surl:
        raise HTTPException(status_code=422, detail="terabox: bad share link")
    host = force_host or _tb_api_host(url)
    try:
        data = _tb_shorturlinfo(host, surl)
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"terabox: lookup failed ({type(e).__name__})")
    if data.get("errno") != 0 and host != "www.terabox.com":
        # Mirror hosts intermittently demand verification (errno
        # 460030); the same share answers on the main host.
        try:
            retry = _tb_shorturlinfo("www.terabox.com", surl)
            if retry.get("errno") == 0:
                data, host = retry, "www.terabox.com"
        except Exception:
            pass
    errno = data.get("errno")
    if errno != 0:
        raise HTTPException(
            status_code=422,
            detail=f"terabox: share lookup refused (errno {errno})")
    files = []

    def walk(items, depth):
        for item in items or []:
            if str(item.get("isdir")) == "1":
                if depth < 3 and len(files) < 50:
                    try:
                        sub = _tb_list_dir(host, surl, item.get("path") or "/")
                    except Exception:
                        sub = []
                    walk(sub, depth + 1)
            else:
                files.append(item)

    walk(data.get("list"), 0)
    # shorturlinfo's own dlink field is always empty; the real links
    # come from sharedownload, keyed by fs_id.
    try:
        links = _tb_sharedownload(host, data,
                                  [it.get("fs_id") for it in files])
    except Exception:
        links = {}
    for it in files:
        if not it.get("dlink"):
            it["dlink"] = links.get(str(it.get("fs_id")))
    files = [f for f in files if f.get("dlink")][:50]
    if not files:
        raise HTTPException(
            status_code=422,
            detail="terabox: no downloadable file (dlink empty)")
    _TB_CACHE[key] = (time.monotonic(), files)
    # Keep the resolution's sign context beside the cache entry, for
    # /fetch's main-host retry (see _tb_sign_context): only the fields
    # sharedownload needs, from the shorturlinfo response that this
    # resolution actually used.
    _TB_INFO[key] = {k: data.get(k) for k in
                     ("sign", "timestamp", "shareid", "uk", "randsk")}
    return files


def _tb_file_ext(item) -> str:
    name = item.get("server_filename") or ""
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _tb_thumb(item):
    thumbs = item.get("thumbs") or {}
    return thumbs.get("url3") or thumbs.get("url2") or thumbs.get("url1")


def resolve_terabox(url: str) -> dict:
    files = _tb_collect_files(url)
    if len(files) > 1:
        # Multi-file share: same playlist shape the yt-dlp path returns;
        # each entry URL carries its fs_id so /fetch can pick the file.
        sep = "&" if "?" in url else "?"
        entries = [{
            "id": str(item.get("fs_id")),
            "title": item.get("server_filename"),
            "url": f"{url}{sep}dtfid={item.get('fs_id')}",
            "duration": None,
            "thumbnail": _tb_thumb(item),
        } for item in files]
        return {"is_playlist": True, "title": "TeraBox share",
                "count": len(entries), "entries": entries}
    item = files[0]
    ext = _tb_file_ext(item)
    name = item.get("server_filename") or "TeraBox file"
    title = name.rsplit(".", 1)[0] if "." in name else name
    size = item.get("size")
    return {
        "title": title,
        "artist": "TeraBox",
        "duration": None,
        "thumbnail": _tb_thumb(item),
        "webpage_url": url,
        "formats": [{
            "id": str(item.get("fs_id")),
            "height": 0,
            "width": None,
            "ext": ext or None,
            "size": int(size) if str(size or "").isdigit() else None,
            "has_audio": ext in TB_VIDEO_EXTS,
        }],
    }


def _tb_dlink_host(dlink: str) -> str:
    """Host of a dlink, for error messages: enough to name which
    TeraBox download host refused, without ever exposing the signed
    dlink itself (or any cookie value)."""
    try:
        return urllib.parse.urlparse(dlink).netloc.lower().split(":")[0] or "?"
    except Exception:
        return "?"


class _TbDownloadRefused(HTTPException):
    """A dlink GET that the download host answered with an HTTP error
    status. Next to the public detail it carries the upstream status
    and the dlink's host as attributes, so fetch_terabox can tell a
    mirror-host 403 (worth one main-host retry) from any other refusal
    without parsing the message text. The detail names only the status
    and the host — never the signed dlink, never a cookie value."""

    def __init__(self, status: int, host: str):
        super().__init__(
            status_code=502,
            detail=f"terabox: download refused (HTTP {status} from {host})")
        self.tb_status = status
        self.tb_host = host


def _tb_download(dlink: str, referer: str):
    """Open a dlink for streaming. Returns (headers, chunk iterator).

    The API hosts accept plain urllib, but the download hosts refuse
    its stock Python TLS fingerprint even with the right cookie and
    Referer (an HTTP error, not a redirect or a timeout), so the GET
    goes through curl_cffi with Chrome impersonation when available
    — libcurl also re-sends the ndus-only Cookie and the Referer on
    every redirect hop toward the CDN, matching the browser. The
    plain-urllib path stays as fallback. Either way, a refusal raises
    an HTTPException naming the upstream status and the dlink's host
    — "download refused (HTTP 403 from d.terabox.com)" — so a
    production failure says which wall it hit (fingerprint block vs
    expired link) instead of a bare, useless "HTTPError"."""
    host = _tb_dlink_host(dlink)
    headers = {
        "User-Agent": TB_UA,
        "Cookie": "ndus=" + TERABOX_NDUS,
        "Referer": referer,
        "Accept": "*/*",
    }
    try:
        from curl_cffi import requests as creq
    except Exception:
        creq = None
    creq_error = None
    if creq is not None:
        try:
            resp = creq.get(dlink, headers=headers, impersonate="chrome",
                            stream=True, timeout=60, allow_redirects=True)
        except Exception as e:
            # Transport-level failure of the impersonated stack: fall
            # through to plain urllib before giving up, so the two
            # stacks' different fates stay visible in the final error.
            creq_error = type(e).__name__
            resp = None
        if resp is not None:
            if resp.status_code != 200:
                status = resp.status_code
                try:
                    resp.close()
                except Exception:
                    pass
                raise _TbDownloadRefused(status, host)

            def chunks():
                try:
                    for part in resp.iter_content(chunk_size=1024 * 256):
                        if part:
                            yield part
                finally:
                    try:
                        resp.close()
                    except Exception:
                        pass

            return resp.headers, chunks()
    req = urllib.request.Request(dlink, headers=headers)
    try:
        resp = _tb_open(req)
    except urllib.error.HTTPError as e:
        raise _TbDownloadRefused(e.code, host)
    except Exception as e:
        extra = (f"; impersonated attempt also failed ({creq_error})"
                 if creq_error else "")
        raise HTTPException(
            status_code=502,
            detail=f"terabox: download failed "
                   f"({type(e).__name__} via {host}){extra}")

    def uchunks():
        try:
            while True:
                part = resp.read(1024 * 256)
                if not part:
                    break
                yield part
        finally:
            resp.close()

    return resp.headers, uchunks()


def _tb_main_host_dlink(url: str, item: dict):
    """Ask www.terabox.com's sharedownload for a main-host dlink for
    this file, reusing the sign context captured during the share's
    natural resolution — no fresh lookup on the main host: its
    shorturlinfo answers a verification demand (errno 460030) from
    this egress even while its sharedownload answers, and a
    d.terabox.com dlink was issued from a reused sign context in the
    first build's local proof. Returns the dlink, or None when the
    main host refuses or its answer carries none. Raises only on
    transport-level failure; the caller treats every outcome alike."""
    info = _tb_sign_context(url)
    if not info:
        return None
    links = _tb_sharedownload("www.terabox.com", info,
                              [item.get("fs_id")])
    return links.get(str(item.get("fs_id")))


def fetch_terabox(u: str, f: str):
    """Stream a TeraBox share's file: re-resolve the share so the dlink
    is fresh, then GET it with only the ndus cookie and proxy it through
    with Content-Disposition: attachment, like the yt-dlp /fetch path.

    One retry: a share resolved on its mirror host carries dlinks from
    that mirror's download host, and a mirror host can refuse the GET
    with HTTP 403 even through the Chrome-impersonated stack while the
    main host serves the same file (production evidence: the mirror's
    d.1024terabox.com refused with 403). The retry does NOT re-resolve
    on the main host — www.terabox.com's shorturlinfo answers a
    verification demand (errno 460030) from this egress — it reuses
    the sign context captured during the natural resolution, asks only
    the main host's sharedownload for a main-host dlink, and retries
    the GET once with it. The detour is silent and never writes to the
    resolution cache: if ANY of its steps fail (no captured context,
    sharedownload refusing, an empty dlink, the retry GET refusing),
    the ORIGINAL mirror refusal surfaces unchanged, with its own
    upstream status and host in the detail — the retry's internal
    errors are not the user-facing truth. Any other first refusal
    (non-403, or already a main-host dlink) also surfaces unchanged."""
    files = _tb_collect_files(u)
    want = (f or "").strip()
    if not want:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
        want = (qs.get("dtfid") or [""])[0]

    def pick(entries):
        return next((c for c in entries if str(c.get("fs_id")) == want),
                    entries[0])

    item = pick(files)
    # A browser reaches the dlink by navigating away from the share
    # page, so the share URL itself is the honest Referer; a caller
    # URL that is somehow not a TeraBox page falls back to the main
    # site root.
    referer = u if is_terabox_url(u) else "https://www.terabox.com/"
    try:
        resp_headers, chunks = _tb_download(item["dlink"], referer)
    except _TbDownloadRefused as refused:
        if (refused.tb_status != 403
                or _tb_dlink_host(item["dlink"]) == "d.terabox.com"):
            raise
        dlink = None
        try:
            dlink = _tb_main_host_dlink(u, item)
        except Exception:
            dlink = None
        if not dlink:
            raise refused
        try:
            resp_headers, chunks = _tb_download(dlink, referer)
        except Exception:
            raise refused
    name = item.get("server_filename") or "terabox-file"
    ext = _tb_file_ext(item)
    stem = name.rsplit(".", 1)[0] if "." in name else name
    filename = safe_name(stem) + ("." + ext if ext else "")
    headers = {
        "Content-Disposition": "attachment; filename*=UTF-8''"
                               + urllib.parse.quote(filename),
    }
    length = resp_headers.get("Content-Length")
    if length:
        headers["Content-Length"] = length
    ctype = resp_headers.get("Content-Type") or "application/octet-stream"
    return StreamingResponse(chunks, media_type=ctype, headers=headers)


# Player-client strategies tried in order for YouTube URLs, as
# (player_client, impersonate) pairs: the first strategy whose extraction
# returns real, playable formats wins; a bot-wall error ("Sign in to
# confirm you're not a bot"), any other extraction failure, or a
# storyboard-only response falls through to the next strategy. With the
# signed-in session loaded, YouTube stopped bot-walling but answered
# every Chrome-impersonated client with storyboard-only formats, so the
# logged-in-friendly clients (android_vr, tv) lead the list, each also
# tried without the Chrome fingerprint. The final entry is the
# long-standing combo, so YouTube behaviour can only improve on the old
# single path, never regress below it.
YT_CLIENT_STRATEGIES = [
    (["tv_downgraded"], True),
    (["tv_downgraded"], False),
    (["android_vr"], True),
    (["tv"], False),
    (["android_vr"], False),
    (["tv"], True),
    (["android"], False),
    (None, True),  # yt-dlp built-in default
    (["ios"], True),
    (["android"], True),
    (["web_safari"], True),
    (["mweb"], True),
    (["android", "tv_embedded", "web_embedded"], True),
]


def _client_label(player_client, impersonate=True) -> str:
    base = "default" if player_client is None else "+".join(player_client)
    return base if impersonate else base + "/noimp"


def cycle_youtube(fn):
    """Run fn(player_client, impersonate) across YT_CLIENT_STRATEGIES;
    first success wins. A result whose format list holds no playable
    video format (a storyboard-only response) counts as a failure, so
    the cycle keeps looking for a client that returns real formats.
    Returns (result, client_label). Re-raises the last DownloadError
    when every strategy fails."""
    last_err = None
    for pc, imp in YT_CLIENT_STRATEGIES:
        label = _client_label(pc, imp)
        try:
            result = fn(pc, imp)
            fmts = result.get("formats") if isinstance(result, dict) else None
            if fmts is not None and not any(
                    f.get("vcodec") not in (None, "none") for f in fmts):
                raise yt_dlp.utils.DownloadError(
                    "no playable formats (storyboard only)")
            print(f"[yt-cycle] OK via {label}", flush=True)
            return result, label
        except yt_dlp.utils.DownloadError as e:
            last_err = e
            print(f"[yt-cycle] failed via {label}: {str(e)[:140]}",
                  flush=True)
    raise last_err


def extract_once(url, extra=None, player_client="KEEP", impersonate=True):
    with yt_dlp.YoutubeDL(ydl_opts(extra, player_client=player_client,
                                   impersonate=impersonate)) as ydl:
        return ydl.extract_info(url, download=False)


# ---------------------------------------------------------------------------
# Invidious fallback for YouTube (the QGrab recipe, 2026-11-10): every
# yt-dlp player client is walled off from this datacenter egress
# (storyboard-only formats, even with the spare account's cookies and a
# PO-token sidecar), but a public Invidious instance is not: its API
# returns the real adaptive formats, and with ?local=true the media URLs
# point at the instance's own /videoplayback proxy, so the file bytes
# flow through the instance instead of straight from googlevideo (which
# refuses this egress). No cookies, no paid proxy. The instance list is
# overridable via env INVIDIOUS_INSTANCES (comma-separated); the default
# is the one API-enabled public instance found on 2026-11-10. Format ids
# are minted as "inv:<itag>" so /fetch can route them back here. Video
# and audio arrive as separate adaptive streams, so /fetch pairs a
# video-only pick with the best m4a audio and muxes with ffmpeg -c copy
# (no re-encode; the free container's CPU could not afford one).
INV_UA = TB_UA
INVIDIOUS_INSTANCES = [
    s.strip() for s in os.environ.get(
        "INVIDIOUS_INSTANCES", "https://yt.chocolatemoo53.com").split(",")
    if s.strip()]
_INV_CACHE = {}
_INV_CACHE_TTL = 600


def _youtube_video_id(url):
    if not url:
        return None
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", url)
    if m:
        return m.group(1)
    m = re.search(r"(?:youtu\.be/|/shorts/|/embed/|/live/|/v/)"
                  r"([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else None


def _inv_api(video_id):
    """Invidious video JSON for video_id (600 s in-memory cache, so a
    /resolve and the /fetch that follows it share one API call). Raises
    the last error when no instance answers with usable formats."""
    import json as _json
    now = time.time()
    hit = _INV_CACHE.get(video_id)
    if hit and now - hit[0] < _INV_CACHE_TTL:
        return hit[1]
    last_err = None
    for inst in INVIDIOUS_INSTANCES:
        api = f"{inst}/api/v1/videos/{video_id}?local=true"
        try:
            req = urllib.request.Request(api, headers={"User-Agent": INV_UA})
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = _json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            last_err = e
            print(f"[invidious] {inst} failed: {str(e)[:120]}", flush=True)
            continue
        if data.get("formatStreams") or data.get("adaptiveFormats"):
            _INV_CACHE[video_id] = (now, data)
            return data
        last_err = yt_dlp.utils.DownloadError(
            "invidious instance returned no formats")
    raise last_err or yt_dlp.utils.DownloadError("invidious unavailable")


def _inv_height(entry):
    for key in ("resolution", "qualityLabel"):
        v = entry.get(key)
        if v:
            m = re.search(r"(\d+)\s*p", str(v))
            if m:
                return int(m.group(1))
    return 0


def _inv_find(data, itag):
    """(entry, is_muxed_stream) for an itag, or (None, False)."""
    for coll, muxed in (("formatStreams", True), ("adaptiveFormats", False)):
        for e in data.get(coll) or []:
            if str(e.get("itag")) == str(itag) and e.get("url"):
                return e, muxed
    return None, False


def resolve_invidious(url):
    """Shape an Invidious API answer into the standard /resolve result."""
    video_id = _youtube_video_id(url)
    if not video_id:
        raise yt_dlp.utils.DownloadError("invidious: no video id in url")
    data = _inv_api(video_id)
    cands = {}

    def offer(entry, has_audio, height):
        ext = entry.get("container") or "mp4"
        typ = entry.get("type") or ""
        if height:
            pref = 2 if (ext == "mp4" and "avc1" in typ) else \
                (1 if ext == "mp4" else 0)
        else:
            pref = 2 if ext == "m4a" else (1 if ext == "mp4" else 0)
        try:
            br = int(entry.get("bitrate") or 0)
        except Exception:
            br = 0
        try:
            size = int(entry.get("clen") or entry.get("size") or 0) or None
        except Exception:
            size = None
        key = (height, ext, has_audio)
        cur = cands.get(key)
        if cur is None or (pref, br) > cur[0]:
            cands[key] = ((pref, br), {
                "id": "inv:" + str(entry.get("itag")),
                "height": height or None,
                "width": None,
                "ext": ext,
                "size": size,
                "has_audio": has_audio,
            })

    for e in data.get("formatStreams") or []:
        offer(e, True, _inv_height(e))
    for e in data.get("adaptiveFormats") or []:
        typ = e.get("type") or ""
        if typ.startswith("audio/"):
            offer(e, True, 0)
        elif typ.startswith("video/"):
            offer(e, False, _inv_height(e))
    formats = [v[1] for v in cands.values()]
    formats.sort(key=lambda x: (x["height"] or 0))
    if not formats:
        raise yt_dlp.utils.DownloadError("invidious: no usable formats")
    # A video-only format is delivered merged with the best audio, so
    # its honest download size is video + audio. Without this the site
    # understated the real file (observed live 2026-11-10: the 360p row
    # showed 15.1 MB while the delivered merged file was 28.8 MB).
    audio_entries = [f for f in formats
                     if f["has_audio"] and not f["height"] and f["size"]]
    if audio_entries:
        # Mirror fetch_invidious's audio choice: m4a first, then size.
        audio_entries.sort(key=lambda f: (f["ext"] == "m4a", f["size"]),
                           reverse=True)
        best_audio = audio_entries[0]["size"]
        for f in formats:
            if not f["has_audio"] and f["size"]:
                f["size"] = f["size"] + best_audio
    thumb = None
    thumbs = data.get("videoThumbnails") or []
    if thumbs:
        thumb = max(thumbs, key=lambda t: t.get("width") or 0).get("url")
    return {
        "title": data.get("title"),
        "artist": data.get("author"),
        "duration": data.get("lengthSeconds"),
        "thumbnail": thumb,
        "webpage_url": url,
        "formats": formats,
        "yt_client": "invidious",
    }


def _inv_download(url, dest):
    """Download one Invidious-proxied media file to dest.

    The media proxy gates on the request looking like a real browser:
    from a plain client it answers HTTP 403 even with the right URL
    (probed 2026-11-10: no-UA and stock-urllib requests are refused,
    a Chrome UA is served). Production containers were still refused
    with a Chrome UA on both the curl_cffi-impersonated and urllib
    stacks, so every attempt's outcome — status plus a snippet of any
    refusal body — is collected and reported in the raised error,
    naming the wall instead of hiding it."""
    headers = {"User-Agent": INV_UA, "Accept": "*/*"}
    cap = 900 * 1024 * 1024
    attempts = []

    def _write(stream):
        total = 0
        with open(dest, "wb") as fh:
            for chunk in stream:
                if not chunk:
                    continue
                total += len(chunk)
                if total > cap:
                    raise HTTPException(status_code=413,
                                        detail="file too large")
                fh.write(chunk)

    def _snip(text):
        return re.sub(r"\s+", " ", (text or ""))[:140]

    try:
        from curl_cffi import requests as creq
    except Exception:
        creq = None
    if creq is not None:
        stalled = False
        for label, hdrs in (("creq-chrome/engine-ua", headers),
                            ("creq-chrome/default-ua", None)):
            if stalled:
                # A stall means the proxy itself is hanging on this
                # media (seen 2026-11-10: one video's streams crawled
                # while another's were fast) — a different UA will not
                # unstick it; fall through to the urllib attempt fast
                # instead of doubling the wait.
                break
            resp = None
            try:
                resp = creq.get(url, headers=hdrs, impersonate="chrome",
                                stream=True, timeout=45,
                                allow_redirects=True)
                if resp.status_code == 200:
                    _write(resp.iter_content(chunk_size=1024 * 256))
                    return dest
                attempts.append(f"{label}: HTTP {resp.status_code} "
                                f"{_snip(resp.text)}")
            except HTTPException:
                raise
            except Exception as e:
                attempts.append(f"{label}: {type(e).__name__}")
                if "timeout" in type(e).__name__.lower() \
                        or "timed out" in str(e).lower():
                    stalled = True
            finally:
                if resp is not None:
                    try:
                        resp.close()
                    except Exception:
                        pass
            try:
                os.remove(dest)
            except OSError:
                pass
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            _write(iter(lambda: resp.read(1024 * 256), b""))
        return dest
    except urllib.error.HTTPError as e:
        try:
            body = e.read(400).decode("utf-8", "replace")
        except Exception:
            body = ""
        attempts.append(f"urllib: HTTP {e.code} {_snip(body)}")
    except Exception as e:
        attempts.append(f"urllib: {type(e).__name__}")
    raise HTTPException(
        status_code=502,
        detail="invidious media refused: " + " | ".join(attempts)[:600])


def fetch_invidious(u, f, wm=""):
    """Deliver an "inv:<itag>" format: muxed streams and audio download
    directly through the instance proxy; a video-only pick is paired
    with the best audio track and muxed with ffmpeg -c copy."""
    import shutil
    import subprocess
    import uuid

    video_id = _youtube_video_id(u)
    if not video_id:
        raise HTTPException(status_code=400, detail="bad youtube url")
    itag = f[4:] if f.startswith("inv:") else f
    try:
        data = _inv_api(video_id)
    except Exception as e:
        raise HTTPException(status_code=502,
                            detail="invidious unavailable: " + str(e)[:150])
    entry, muxed = _inv_find(data, itag)
    if entry is None:
        raise HTTPException(status_code=422,
                            detail="invidious format not found")
    title = data.get("title") or "video"
    workdir = os.path.join("/tmp/dt-inv", uuid.uuid4().hex)
    os.makedirs(workdir, exist_ok=True)
    typ = entry.get("type") or ""
    ext = entry.get("container") or "mp4"
    try:
        if muxed or typ.startswith("audio/"):
            kind = "audio" if typ.startswith("audio/") else "video"
            path = os.path.join(workdir, "media." + ext)
            _inv_download(entry["url"], path)
            out_ext = ext
            media = kind + "/" + ("mp4" if ext == "m4a" else ext)
        else:
            audio = None
            for e in data.get("adaptiveFormats") or []:
                if (e.get("type") or "").startswith("audio/") and e.get("url"):
                    score = (1 if e.get("container") == "m4a" else 0,
                             int(e.get("bitrate") or 0))
                    if audio is None or score > audio[0]:
                        audio = (score, e)
            if audio is None:
                raise HTTPException(status_code=422,
                                    detail="invidious: no audio track")
            audio = audio[1]
            vsrc = os.path.join(workdir, "video." + ext)
            asrc = os.path.join(workdir,
                                "audio." + (audio.get("container") or "m4a"))
            # Video and audio come from the same proxy — fetch them in
            # parallel so a slow stream does not double the wait
            # (serial fetches pushed some videos past every timeout
            # downstream, 2026-11-10).
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=2) as pool:
                fut_v = pool.submit(_inv_download, entry["url"], vsrc)
                fut_a = pool.submit(_inv_download, audio["url"], asrc)
                fut_v.result()
                fut_a.result()
            out_ext = "mp4" if (ext == "mp4"
                                and audio.get("container") == "m4a") \
                else "mkv"
            path = os.path.join(workdir, "merged." + out_ext)
            proc = subprocess.run(
                ["ffmpeg", "-y", "-i", vsrc, "-i", asrc, "-c", "copy", path],
                capture_output=True, timeout=300)
            if proc.returncode != 0 or not os.path.exists(path):
                raise HTTPException(status_code=422,
                                    detail="ffmpeg merge failed")
            media = "video/mp4" if out_ext == "mp4" else "video/x-matroska"
    except HTTPException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(status_code=502,
                            detail="invidious download failed: "
                            + str(e)[:150])
    if wm == "1" and media.startswith("video/") and out_ext == "mp4":
        path = _stamp_watermark(path, data.get("lengthSeconds"))
    filename = safe_name(title) + "." + out_ext
    size = os.path.getsize(path)

    def stream():
        try:
            with open(path, "rb") as fh:
                while True:
                    chunk = fh.read(1024 * 256)
                    if not chunk:
                        break
                    yield chunk
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    return StreamingResponse(
        stream(),
        media_type=media,
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''"
                                   + urllib.parse.quote(filename),
            "Content-Length": str(size),
        },
    )


def safe_name(title: str) -> str:
    name = re.sub(r"[^\w\s\-\u0600-\u06FF]", "", title or "video").strip()
    return (name[:80] or "video")


@app.get("/health")
def health():
    return {"ok": True, "engine": "yt-dlp " + yt_dlp.version.__version__}


def extract_playlist(url: str, player_client="KEEP", impersonate=True) -> dict:
    """Flat playlist extraction: list up to 50 entries without resolving
    each video, so playlist/collection URLs return something useful.
    Each entry's url is the watch URL /fetch can download directly."""
    opts = ydl_opts({"extract_flat": "in_playlist", "playlistend": 50},
                    player_client=player_client, impersonate=impersonate)
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
                lambda pc, imp: extract_playlist(
                    url, player_client=pc, impersonate=imp))
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
    if is_terabox_url(url):
        # yt-dlp has no TeraBox extractor; the sharing-API branch
        # raises its own clean HTTPExceptions on any refusal.
        return resolve_terabox(url)
    yt_client = None
    try:
        if is_youtube_url(url):
            info, yt_client = cycle_youtube(
                lambda pc, imp: extract_once(
                    url, player_client=pc, impersonate=imp))
        else:
            info = extract_once(url)
    except yt_dlp.utils.DownloadError as e:
        if is_youtube_url(url):
            # Every yt-dlp client is walled off from this egress; the
            # Invidious fallback speaks for YouTube instead. Its own
            # failure leaves the original honest error standing.
            try:
                return resolve_invidious(url)
            except Exception as inv_e:
                print(f"[invidious] resolve fallback failed: "
                      f"{str(inv_e)[:140]}", flush=True)
        raise HTTPException(status_code=422, detail=str(e)[:300])
    if info.get("_type") == "playlist" or "entries" in info:
        # Playlist URL: the single-video path yields zero formats, so
        # answer with the playlist listing instead.
        try:
            if is_youtube_url(url):
                result, label = cycle_youtube(
                    lambda pc, imp: extract_playlist(
                        url, player_client=pc, impersonate=imp))
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


def _stamp_watermark(path, known_duration=None):
    """Burn the 'Dahot Tool' brand watermark into a short MP4 — the
    Free-plan brand mark (added 2026-11-10). SAFETY-FIRST: returns the
    ORIGINAL path on any problem (not an MP4, duration unknown or over
    the 4-minute cap, no usable font, ffmpeg error or timeout) — a
    watermark must never break or long-delay a download. The caller
    passes wm=1 only for Free-plan fetches (decided server-side in the
    engine-proxy from the caller's real plan).

    SPEED GATE (2026-11-10, measured locally under a 0.25-CPU duty
    throttle): a 720p ultrafast/CRF32 stamp encode needs ~1 wall-second
    per video-second AT BEST on a quarter core, and the main engine's
    host kills any request at ~90 s — so the old flat 50 s deadline
    still let download + probe + a doomed encode stack past 90 s and
    the download died with a 504. Now the stamp is only ATTEMPTED when
    the duration (passed in by the caller from resolve metadata, so no
    ffprobe is needed) says the encode can plausibly finish inside a
    small budget: duration x DT_WM_RATE (planning wall-seconds per
    video-second, default 1.5) must fit DT_WM_BUDGET_S (default 25 s),
    which is also the encode's hard kill deadline. Anything longer
    returns the ORIGINAL immediately — zero added delay, never a 504.
    On a stronger host (e.g. the Railway YouTube engine) both knobs can
    be raised via env to admit longer videos."""
    import subprocess
    budget = 25.0
    rate = 1.5
    try:
        budget = float(os.environ.get("DT_WM_BUDGET_S", "25") or 25)
        rate = float(os.environ.get("DT_WM_RATE", "1.5") or 1.5)
    except Exception:
        pass
    try:
        if not path.lower().endswith(".mp4"):
            return path
        dur = 0.0
        if known_duration:
            try:
                dur = float(known_duration)
            except Exception:
                dur = 0.0
        if not dur:
            # Fallback only: probe the local file (measured ~0.1 s;
            # the old 30 s timeout was pure worst-case stack-up).
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries",
                 "format=duration", "-of", "csv=p=0", path],
                capture_output=True, timeout=8)
            dur = float((probe.stdout or b"").decode().strip() or 0)
        if not dur or dur > 240:
            return path
        if dur * rate > budget:
            # The encode cannot finish inside the budget on this
            # class of host — serve the original NOW instead of
            # burning the whole deadline and risking a 504.
            return path
        font = ""
        for cand in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                     "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"):
            if os.path.exists(cand):
                font = f"fontfile={cand}:"
                break
        draw = (f"drawtext={font}text='Dahot Tool':fontcolor=white:"
                f"fontsize=h/34:borderw=1:bordercolor=black@0.3:"
                f"box=1:boxcolor=0x0B5563@0.40:boxborderw=7:"
                f"x=w-tw-12:y=h-th-12")
        out = path[:-4] + ".wm.mp4"
        # HARD DEADLINE = the same budget the gate planned against;
        # if ffmpeg is not done by then it is killed and the ORIGINAL
        # file is served — a download must never fail, or wait the
        # full budget plus a slow download, because of a watermark.
        # (No +faststart: it costs a second full read+write pass over
        # the output and a download attachment does not need it.)
        proc = subprocess.Popen(
            ["ffmpeg", "-y", "-i", path, "-vf", draw,
             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "32",
             "-maxrate", "2000k", "-bufsize", "4000k",
             "-c:a", "copy", out],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            rc = proc.wait(timeout=budget)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=3)
            except Exception:
                pass
            try:
                os.remove(out)
            except OSError:
                pass
            return path
        if rc == 0 and os.path.exists(out) \
                and os.path.getsize(out) > 0:
            try:
                os.remove(path)
            except OSError:
                pass
            return out
    except Exception:
        pass
    return path


@app.get("/fetch")
def fetch(u: str, f: str = "", wm: str = "",
          x_dt_key: str = Header(default="")):
    check_key(x_dt_key)
    if not re.match(r"^https?://", u or ""):
        raise HTTPException(status_code=400, detail="bad url")
    if is_terabox_url(u):
        # Re-resolve the share and stream the fresh dlink (see above).
        return fetch_terabox(u, f)
    if is_youtube_url(u) and f.startswith("inv:"):
        # Format ids minted by the Invidious /resolve fallback.
        return fetch_invidious(u, f, wm)
    if f:
        # A bare format id from /resolve can be a video-only track
        # (Reddit/DASH and YouTube adaptive formats): try it merged
        # with the best audio first, fall back to the bare id — the
        # bare id alone would deliver a silent file (proven with a
        # native Reddit video, 2026-11-10).
        fmt = f"{f}+ba/{f}"
    else:
        fmt = "bv*[height<=720]+ba/b[height<=720]/b"
    tmpdir = "/tmp/dt-fetch"
    os.makedirs(tmpdir, exist_ok=True)
    extra = {
        "format": fmt,
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
        "max_filesize": 900 * 1024 * 1024,
    }

    def _download(opts):
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(u, download=True)
            path = ydl.prepare_filename(info)
            if not os.path.exists(path):  # merged file may switch extension
                base = os.path.splitext(path)[0]
                for cand in (base + ".mp4", base + ".mkv", base + ".webm"):
                    if os.path.exists(cand):
                        path = cand
                        break
            if not os.path.exists(path):
                # HLS downloads (e.g. Vimeo) land under the plain
                # outtmpl name while prepare_filename points at the
                # pre-merge ".f<format_id>" name — find the file this
                # download actually produced (same video id prefix,
                # newest first) instead of failing (proven 2026-11-10
                # with a Vimeo Staff Pick: download completed, then
                # /fetch 500'd on the missing expected path).
                import glob as _glob
                cands = [
                    c for c in _glob.glob(
                        os.path.join(tmpdir, (info.get("id") or "") + "*"))
                    if os.path.splitext(c)[1] in (".mp4", ".mkv", ".webm")
                    and os.path.getsize(c) > 0
                ]
                if cands:
                    path = max(cands, key=lambda c: os.path.getmtime(c))
            return path, (info.get("title") or "video"), info.get("duration")

    try:
        if is_youtube_url(u):
            # Download through the same client cycle as /resolve: the
            # client that can see the real formats is the one that can
            # fetch them.
            (path, title, dur_s), _label = cycle_youtube(
                lambda pc, imp: _download(
                    ydl_opts(extra, player_client=pc, impersonate=imp)))
        else:
            path, title, dur_s = _download(ydl_opts(extra))
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=422, detail=str(e)[:300])
    if not os.path.exists(path):
        raise HTTPException(status_code=500, detail="file missing after download")

    if wm == "1":
        path = _stamp_watermark(path, dur_s)
    filename = safe_name(title) + os.path.splitext(path)[1]
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


def _subs_main(payload: dict):
    """yt-dlp subtitle extraction (the original /subs body). Raises
    HTTPException on any failure; the routed wrapper below adds the
    Invidious captions fallback for YouTube URLs."""
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
                lambda pc, imp: extract_once(
                    url, player_client=pc, impersonate=imp))
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


def subs_invidious(url, lang):
    """Captions via the Invidious instance for a YouTube video — the
    fallback when the yt-dlp caption route is walled (added 2026-11-10,
    the same evening the Invidious download route went live; the video
    JSON carries a captions list whose files the instance serves
    itself, so a datacenter client can read them)."""
    import json as _json
    video_id = _youtube_video_id(url)
    if not video_id:
        raise HTTPException(status_code=400, detail="bad youtube url")
    last_err = None
    for inst in INVIDIOUS_INSTANCES:
        try:
            req = urllib.request.Request(
                f"{inst}/api/v1/videos/{video_id}",
                headers={"User-Agent": INV_UA})
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = _json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            last_err = e
            continue
        captions = data.get("captions") or []
        if not captions:
            raise HTTPException(status_code=404,
                                detail="no subtitles available")
        def _code(c):
            return c.get("languageCode") or c.get("language_code") or ""
        def _label(c):
            return c.get("label") or ""
        pick = None
        if lang:
            pick = next((c for c in captions if _code(c) == lang), None) \
                or next((c for c in captions
                         if _label(c).lower().startswith(lang.lower())),
                        None)
        if pick is None:
            pick = next((c for c in captions
                         if _code(c) == "en"
                         and "auto" not in _label(c).lower()), None) \
                or next((c for c in captions if _code(c) == "en"), None) \
                or captions[0]
        cap_url = pick.get("url") or ""
        if not cap_url:
            raise HTTPException(status_code=404,
                                detail="no subtitles available")
        if cap_url.startswith("/"):
            cap_url = inst + cap_url
        raw = None
        try:
            req = urllib.request.Request(cap_url,
                                         headers={"User-Agent": INV_UA})
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except Exception:
            try:
                from curl_cffi import requests as creq
                r = creq.get(cap_url, headers={"User-Agent": INV_UA},
                             impersonate="chrome", timeout=30)
                if r.status_code == 200:
                    raw = r.text
            except Exception:
                raw = None
        if raw is None:
            raise HTTPException(status_code=502,
                                detail="subtitle fetch failed")
        lines = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or "-->" in line or line.startswith(
                    ("WEBVTT", "Kind:", "Language:")):
                continue
            if line.isdigit():
                continue
            line = re.sub(r"<[^>]+>", "", line)
            if line and (not lines or lines[-1] != line):
                lines.append(line)
        text = "\n".join(lines)
        if not text:
            raise HTTPException(status_code=404,
                                detail="no subtitles available")
        return {"title": data.get("title"), "lang": _code(pick),
                "available": sorted({_code(c) for c in captions if _code(c)}),
                "text": text, "yt_client": "invidious"}
    raise HTTPException(status_code=502,
                        detail="invidious unavailable: "
                        + str(last_err)[:150])


@app.post("/subs")
def subs(payload: dict, x_dt_key: str = Header(default="")):
    """Subtitle / transcript extraction for the Subtitle Downloader and
    YouTube Transcript tools. Returns the plain text of the best
    available subtitle track (manual subtitles first, then automatic
    captions). For YouTube URLs whose yt-dlp route fails, captions are
    served through the Invidious fallback instead."""
    check_key(x_dt_key)
    url = (payload or {}).get("url", "").strip()
    try:
        return _subs_main(payload)
    except HTTPException:
        if is_youtube_url(url):
            return subs_invidious(url, (payload or {}).get("lang", "").strip())
        raise


@app.post("/convert")
async def convert(request: Request, mode: str = "mp3", name: str = "video",
                  start: float = 0.0, dur: float = 5.0,
                  x_dt_key: str = Header(default="")):
    # File conversion for the site's Video->MP3 and Video->GIF tools.
    # The uploaded file arrives as the RAW request body (no multipart, so
    # no extra dependency); mode/start/dur/name ride in the query string.
    # ffmpeg does the work; the result streams back as an attachment.
    check_key(x_dt_key)
    import asyncio
    import shutil
    import subprocess
    import uuid

    mode = (mode or "mp3").lower()
    if mode not in ("mp3", "gif", "enhance"):
        raise HTTPException(status_code=400, detail="bad mode")
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty upload")
    if len(data) > 100 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="file too large (100 MB max)")

    base = safe_name(os.path.splitext(name or "video")[0] or "video")
    workdir = os.path.join("/tmp/dt-convert", uuid.uuid4().hex)
    os.makedirs(workdir, exist_ok=True)
    src = os.path.join(workdir, "input.bin")
    with open(src, "wb") as fh:
        fh.write(data)

    if mode == "mp3":
        out = os.path.join(workdir, "output.mp3")
        cmd = ["ffmpeg", "-y", "-i", src, "-vn",
               "-codec:a", "libmp3lame", "-q:a", "2", out]
        media, fname = "audio/mpeg", base + ".mp3"
    elif mode == "enhance":
        # Voice Enhancer: reduce steady background noise, lift the
        # voice band and normalise loudness. Real DSP (ffmpeg afftdn +
        # loudnorm) — honest "cleaner and clearer", not magic.
        out = os.path.join(workdir, "output.mp3")
        af = ("highpass=f=80,afftdn=nr=12:nf=-25,"
              "loudnorm=I=-16:TP=-1.5:LRA=11")
        cmd = ["ffmpeg", "-y", "-i", src, "-vn", "-af", af,
               "-codec:a", "libmp3lame", "-q:a", "2", out]
        media, fname = "audio/mpeg", base + "-enhanced.mp3"
    else:
        start = max(0.0, min(float(start or 0), 3600.0))
        dur = max(1.0, min(float(dur or 5), 10.0))
        out = os.path.join(workdir, "output.gif")
        vf = ("fps=12,scale=480:-1:flags=lanczos,"
              "split[a][b];[a]palettegen[p];[b][p]paletteuse")
        cmd = ["ffmpeg", "-y", "-ss", str(start), "-t", str(dur),
               "-i", src, "-vf", vf, out]
        media, fname = "image/gif", base + ".gif"

    try:
        proc = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, timeout=240)
    except Exception as e:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(status_code=500,
                            detail="conversion failed to start: " + str(e)[:200])
    if proc.returncode != 0 or not os.path.exists(out):
        tail = (proc.stderr or b"").decode("utf-8", "replace")[-300:]
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(status_code=422,
                            detail="ffmpeg could not convert this file: " + tail)

    def chunks():
        try:
            with open(out, "rb") as fh:
                while True:
                    block = fh.read(256 * 1024)
                    if not block:
                        break
                    yield block
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    return StreamingResponse(
        chunks(), media_type=media,
        headers={"Content-Disposition": 'attachment; filename="' + fname + '"'})


def _tts_chunks(text, limit=180):
    """Split text into <=limit-char chunks on sentence boundaries."""
    import re as _re
    parts = _re.split(r"(?<=[.!?\n\u06d4])\s+", text.strip())
    chunks, cur = [], ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        while len(p) > limit:
            cut = p.rfind(" ", 0, limit)
            if cut < 40:
                cut = limit
            chunks.append(p[:cut].strip())
            p = p[cut:].strip()
        if len(cur) + len(p) + 1 <= limit:
            cur = (cur + " " + p).strip()
        else:
            if cur:
                chunks.append(cur)
            cur = p
    if cur:
        chunks.append(cur)
    return chunks[:12]


@app.post("/tts")
def tts(payload: dict, x_dt_key: str = Header(default="")):
    """AI Voiceover: real speech from text (Google Translate TTS route —
    free, no key). Returns one MP3 (chunks concatenated)."""
    check_key(x_dt_key)
    text = (payload.get("text") or "").strip()
    lang = (payload.get("lang") or "en").strip().lower()[:5] or "en"
    if not text:
        raise HTTPException(400, "No text given.")
    if len(text) > 1200:
        text = text[:1200]
    audio = b""
    for chunk in _tts_chunks(text):
        q = urllib.parse.urlencode(
            {"ie": "UTF-8", "q": chunk, "tl": lang, "client": "tw-ob"})
        req = urllib.request.Request(
            "https://translate.google.com/translate_tts?" + q,
            headers={"User-Agent": TB_UA, "Referer": "https://translate.google.com/"})
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = resp.read()
        except Exception as e:
            raise HTTPException(502, f"Voice service failed: {e}")
        if len(data) < 200:
            raise HTTPException(502, "Voice service returned no audio.")
        audio += data
    if len(audio) < 500:
        raise HTTPException(502, "Voice service returned no audio.")
    return Response(content=audio, media_type="audio/mpeg", headers={
        "Content-Disposition": 'attachment; filename="dahot-voiceover.mp3"'})


@app.post("/thumbs")
def thumbs(payload: dict, x_dt_key: str = Header(default="")):
    """Naam-search result covers for TikTok (added 2026-11-11 at
    Rashid's order: search cards showed only the platform logo).
    First route was the tikwm API, but tikwm refuses this engine's
    egress AND the edge function's egress (only the first call went
    through there; both engines got nulls) — while this engine's own
    yt-dlp extraction resolves TikTok fine (it powers downloads), so
    covers are taken from the extracted info's thumbnail field.
    Body: {urls: [...]} (max 10). Returns {thumbs: {url: cover-or-
    null}}. Extractions run 4 at a time (~2s each measured live);
    a failure for any URL just yields null for that URL (the site
    then shows the platform tile), never an error for the batch."""
    check_key(x_dt_key)
    urls = payload.get("urls") or []
    if not isinstance(urls, list):
        raise HTTPException(400, "urls must be a list.")
    links = [str(u or "") for u in urls[:10]]

    def one(link: str):
        if "tiktok.com" not in link:
            return link, None
        try:
            info = extract_once(link)
            t = str(info.get("thumbnail") or "")
            return link, (t if t.startswith("http") else None)
        except Exception:
            return link, None

    out = {}
    if links:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as pool:
            for link, cover in pool.map(one, links):
                out[link] = cover
    return {"thumbs": out}


# ---------------------------------------------------------------------------
# AI Video / AI Music generators.
#
# Both tools run on public Hugging Face Spaces via the anonymous Gradio
# queue API (no key, no account, no card — the same class of route as the
# AI Picture tool): AI Video = Lightricks LTX-Video distilled, AI Music =
# ACE-Step v1. Routes were proven with real output files 2026-11-10 (see
# hidden_files/ai-gen-routes-research-2026-11-10.md). A generation can sit
# in the Space's GPU queue far longer than this host's ~90 s request
# limit, so generation is a JOB, never one long request:
#   POST /genvideo {"prompt": ...}            -> {"job": id} at once
#   POST /genmusic {"prompt": ..., "duration": seconds?}
#   GET  /genjob/<id>  -> {"state": "working"} while the job runs,
#                         {"state": "error", "detail": ...} on failure,
#                         or the finished FILE ITSELF (200 + attachment)
#                         once done.
# A failed generation never produces a fake file; the site shows the
# honest detail and does not consume the user's free try.
# ---------------------------------------------------------------------------
import json as _json
import threading as _threading
import uuid as _uuid

GEN_DIR = "/tmp/dt-genjobs"
GEN_MAX_ACTIVE = 2      # concurrent jobs; the anonymous GPU budget is thin
GEN_KEEP_RESULTS = 10   # newest finished job files kept for pollers
GEN_JOB_TTL = 15 * 60   # seconds before a job (and its file) is dropped

LTX_SPACE = "https://lightricks-ltx-video-distilled.hf.space"
LTX_FN_INDEX = 4        # config dependency index of api_name "text_to_video"
ACE_SPACE = "https://ace-step-ace-step.hf.space"
ACE_FN_INDEX = 11       # config dependency index of api_name "__call__"
QWEN_SPACE = "https://qwen-qwen-image-edit.hf.space"
QWEN_FN_INDEX = 2       # config dependency index of api_name "infer"
EDIT_MAX_BYTES = 4 * 1024 * 1024    # decoded-image cap for /editimage

_gen_jobs = {}          # id -> {kind, state, created, path, media, filename, detail}
_gen_lock = _threading.Lock()

try:
    os.makedirs(GEN_DIR, exist_ok=True)
except Exception:
    pass


class _GenFail(Exception):
    """A generation failure with a short, honest, caller-safe detail.
    transient=True marks transport-level trouble (worth ONE retry)."""

    def __init__(self, detail, transient=False):
        super().__init__(detail)
        self.detail = detail
        self.transient = transient


def _gen_housekeeping_locked():
    """Drop expired jobs/files and cap kept results. Caller holds the lock."""
    now = time.time()
    for job_id in list(_gen_jobs):
        job = _gen_jobs[job_id]
        if now - job["created"] > GEN_JOB_TTL:
            if job.get("path"):
                try:
                    os.remove(job["path"])
                except Exception:
                    pass
            del _gen_jobs[job_id]
    done = sorted((j for j in _gen_jobs.values() if j["state"] == "done"),
                  key=lambda j: j["created"])
    for job in done[:-GEN_KEEP_RESULTS] if len(done) > GEN_KEEP_RESULTS else []:
        if job.get("path"):
            try:
                os.remove(job["path"])
            except Exception:
                pass
        _gen_jobs.pop(job["id"], None)
    # Sweep orphaned files (e.g. a job removed while its worker was writing).
    try:
        live = {j.get("path") for j in _gen_jobs.values()}
        for name in os.listdir(GEN_DIR):
            path = os.path.join(GEN_DIR, name)
            if path not in live and now - os.path.getmtime(path) > GEN_JOB_TTL:
                os.remove(path)
    except Exception:
        pass


def _gen_extract_file_url(space, output):
    """Pull the result file URL out of a completed Gradio output. The
    file can sit directly in data[0] (ACE-Step: a FileData dict) or
    nested one level down (LTX: data[0] is {"video": FileData,
    "subtitles": ...}), so search recursively — the same walk the
    research's working test script used."""

    def find(node):
        if isinstance(node, dict):
            if node.get("url"):
                return node["url"]
            if node.get("path"):
                return space + "/gradio_api/file=" + node["path"]
            for value in node.values():
                hit = find(value)
                if hit:
                    return hit
        elif isinstance(node, (list, tuple)):
            for value in node:
                hit = find(value)
                if hit:
                    return hit
        elif isinstance(node, str) and node.startswith("http"):
            return node
        return None

    return find((output or {}).get("data"))


def _gen_error_detail(err_text):
    """Map a Space-side error to a short honest detail. Returns
    (detail, transient)."""
    low = (err_text or "").lower()
    if "quota" in low or "zerogpu" in low:
        return ("The free generation service is busy right now — "
                "please try again later."), False
    return "The generation failed on the service side. Please try again.", False


def _hf_auth_headers(base=None):
    """Headers for Hugging Face Space calls. When the HF_TOKEN env var
    is set (Rashid's own Hugging Face account token — he approved this
    route 2026-11-11), calls carry it so the Spaces' ZeroGPU time is
    billed to his account's larger free quota instead of the tiny
    anonymous budget that kept running out during the 2026-11-11
    rechecks. Without the env var, behaviour is exactly as before."""
    h = dict(base or {})
    tok = os.environ.get("HF_TOKEN", "").strip()
    if tok:
        h["Authorization"] = "Bearer " + tok
    return h


def _hf_sse_once(space, session_hash, deadline):
    """One SSE pass over the Gradio queue/data stream. Returns the result
    file URL on completion, None when the stream ends/drops without a
    terminal message (the caller reconnects), and raises _GenFail for
    failures the service itself reported."""
    url = space + "/gradio_api/queue/data?session_hash=" + session_hash
    req = urllib.request.Request(url, headers=_hf_auth_headers({"User-Agent": TB_UA}))
    try:
        resp = urllib.request.urlopen(
            req, timeout=max(5.0, min(45.0, deadline - time.time())))
    except Exception:
        return None
    with resp:
        while True:
            if time.time() >= deadline:
                raise _GenFail(
                    "The generation took too long and was stopped. "
                    "Please try again.")
            try:
                line = resp.readline()
            except Exception:
                return None
            if not line:
                return None
            line = line.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                msg = _json.loads(line[5:].strip())
            except Exception:
                continue
            kind = msg.get("msg") or ""
            if kind == "process_completed":
                output = msg.get("output") or {}
                err = output.get("error") or msg.get("error")
                if err or msg.get("success") is False:
                    detail, transient = _gen_error_detail(str(err or ""))
                    raise _GenFail(detail, transient=transient)
                file_url = _gen_extract_file_url(space, output)
                if not file_url:
                    raise _GenFail(
                        "The generation finished without a file. "
                        "Please try again.")
                return file_url
            if kind in ("process_error", "error"):
                detail, transient = _gen_error_detail(
                    str(msg.get("error") or ""))
                raise _GenFail(detail, transient=transient)
            # estimation / process_starts / process_generating / heartbeat:
            # keep waiting.


def _hf_queue_file_url(space, fn_index, data, deadline):
    """queue/join -> SSE wait -> result file URL (the raw-HTTP pattern
    proven in the 2026-11-10 research; gradio_client's long-poll is not
    used because it dies on some egress proxies)."""
    session_hash = _uuid.uuid4().hex[:12]
    body = _json.dumps({"data": data, "fn_index": fn_index,
                        "session_hash": session_hash}).encode("utf-8")
    req = urllib.request.Request(
        space + "/gradio_api/queue/join", data=body, method="POST",
        headers=_hf_auth_headers({"User-Agent": TB_UA, "Content-Type": "application/json"}))
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            join = _json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        raise _GenFail("The generation service could not be reached. "
                       "Please try again.", transient=True)
    if not join.get("event_id"):
        raise _GenFail("The free generation service is busy right now — "
                       "please try again later.", transient=True)
    reconnects = 0
    while True:
        if time.time() >= deadline:
            raise _GenFail("The generation took too long and was stopped. "
                           "Please try again.")
        file_url = _hf_sse_once(space, session_hash, deadline)
        if file_url:
            return file_url
        reconnects += 1
        if reconnects > 12:
            raise _GenFail("The generation service could not be reached. "
                           "Please try again.", transient=True)
        time.sleep(2)


def _gen_download(job_id, kind, file_url, deadline):
    """Download the finished file into GEN_DIR. Returns
    (path, media_type, filename)."""
    exts = (".mp4", ".webm", ".mov") if kind == "video" else \
        (".mp3", ".wav", ".flac", ".ogg") if kind == "music" else \
        (".png", ".jpg", ".jpeg", ".webp")
    ext = os.path.splitext(urllib.parse.urlparse(file_url).path)[1].lower()
    if ext not in exts:
        ext = exts[0]
    media = {"video": {".mp4": "video/mp4", ".webm": "video/webm",
                       ".mov": "video/quicktime"},
             "music": {".mp3": "audio/mpeg", ".wav": "audio/wav",
                       ".flac": "audio/flac", ".ogg": "audio/ogg"},
             "image": {".png": "image/png", ".jpg": "image/jpeg",
                       ".jpeg": "image/jpeg", ".webp": "image/webp"}}[kind][ext]
    fname = {"video": "dahot-ai-video", "music": "dahot-ai-music",
             "image": "dahot-ai-photo-edit"}[kind] + ext
    path = os.path.join(GEN_DIR, job_id + ext)
    req = urllib.request.Request(file_url, headers=_hf_auth_headers({"User-Agent": TB_UA}))
    try:
        with urllib.request.urlopen(
                req, timeout=max(10.0, min(60.0, deadline - time.time()))) as resp, \
                open(path, "wb") as fh:
            while True:
                block = resp.read(256 * 1024)
                if not block:
                    break
                fh.write(block)
    except Exception:
        raise _GenFail("The generated file could not be downloaded. "
                       "Please try again.", transient=True)
    if not os.path.exists(path) or os.path.getsize(path) < 1024:
        raise _GenFail("The generation returned an empty file. "
                       "Please try again.", transient=True)
    return path, media, fname


def _gen_worker(job_id, kind, space, fn_index, data, timeout_s):
    deadline = time.time() + timeout_s
    detail = "The generation failed. Please try again."
    for _attempt in (1, 2):     # one automatic retry, transient failures only
        try:
            file_url = _hf_queue_file_url(space, fn_index, data, deadline)
            path, media, fname = _gen_download(job_id, kind, file_url, deadline)
            with _gen_lock:
                job = _gen_jobs.get(job_id)
                if job:
                    job.update(state="done", path=path, media=media,
                               filename=fname)
            return
        except _GenFail as e:
            detail = e.detail
            if not e.transient:
                break
        except Exception:
            detail = ("The generation service could not be reached. "
                      "Please try again.")
        if time.time() >= deadline:
            break
        time.sleep(2)
    with _gen_lock:
        job = _gen_jobs.get(job_id)
        if job:
            job.update(state="error", detail=detail)


def _hf_upload_file(space, file_path, filename, mime):
    """Upload one local file to a Gradio Space (POST /gradio_api/upload)
    and return the server-side path the queue/join data must reference
    for a file input. Raises _GenFail(transient=True) on transport
    trouble, like the rest of the HF helpers."""
    boundary = "----dahot" + _uuid.uuid4().hex[:16]
    with open(file_path, "rb") as fh:
        blob = fh.read()
    body = b"".join([
        ("--" + boundary + "\r\n").encode("utf-8"),
        ('Content-Disposition: form-data; name="files"; filename="' +
         filename + '"\r\n').encode("utf-8"),
        ("Content-Type: " + mime + "\r\n\r\n").encode("utf-8"),
        blob,
        b"\r\n",
        ("--" + boundary + "--\r\n").encode("utf-8"),
    ])
    req = urllib.request.Request(
        space + "/gradio_api/upload", data=body, method="POST",
        headers=_hf_auth_headers(
            {"User-Agent": TB_UA,
             "Content-Type": "multipart/form-data; boundary=" + boundary}))
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            got = _json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        raise _GenFail("The generation service could not be reached. "
                       "Please try again.", transient=True)
    if not got or not isinstance(got, list) or not got[0]:
        raise _GenFail("The generation service could not be reached. "
                       "Please try again.", transient=True)
    return got[0]


def _edit_worker(job_id, kind, space, fn_index, data, timeout_s):
    """Worker for /editimage jobs: upload the caller's photo to the
    Qwen-Image-Edit Space, run its queue with the edit instruction,
    download the edited image. Same retry contract as _gen_worker; the
    temp upload file is always removed afterwards."""
    deadline = time.time() + timeout_s
    detail = "The generation failed. Please try again."
    tmp_path = data["tmp_path"]
    try:
        for _attempt in (1, 2):     # one automatic retry, transient only
            try:
                up_path = _hf_upload_file(space, tmp_path,
                                          data["filename"], data["mime"])
                filedata = {"path": up_path, "orig_name": data["filename"],
                            "mime_type": data["mime"],
                            "meta": {"_type": "gradio.FileData"}}
                # Parameter order/defaults from the Space's live config
                # (dependency "infer", verified 2026-11-20): image,
                # prompt, seed, randomize seed, true guidance scale,
                # inference steps, rewrite prompt.
                join_data = [filedata, data["prompt"], 0, True, 4.0, 50, True]
                file_url = _hf_queue_file_url(space, fn_index, join_data,
                                              deadline)
                path, media, fname = _gen_download(job_id, kind, file_url,
                                                   deadline)
                with _gen_lock:
                    job = _gen_jobs.get(job_id)
                    if job:
                        job.update(state="done", path=path, media=media,
                                   filename=fname)
                return
            except _GenFail as e:
                detail = e.detail
                if not e.transient:
                    break
            except Exception:
                detail = ("The generation service could not be reached. "
                          "Please try again.")
            if time.time() >= deadline:
                break
            time.sleep(2)
        with _gen_lock:
            job = _gen_jobs.get(job_id)
            if job:
                job.update(state="error", detail=detail)
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass


def _gen_start(kind, space, fn_index, data, timeout_s, worker=None):
    with _gen_lock:
        _gen_housekeeping_locked()
        active = sum(1 for j in _gen_jobs.values() if j["state"] == "working")
        if active >= GEN_MAX_ACTIVE:
            raise HTTPException(
                503, "The generation service is busy right now — "
                     "please try again in a moment.")
        job_id = _uuid.uuid4().hex[:16]
        _gen_jobs[job_id] = {"id": job_id, "kind": kind, "state": "working",
                             "created": time.time(), "path": None,
                             "media": None, "filename": None, "detail": None}
    _threading.Thread(target=worker or _gen_worker,
                      args=(job_id, kind, space, fn_index, data, timeout_s),
                      daemon=True).start()
    return {"job": job_id}


@app.post("/genvideo")
def genvideo(payload: dict, x_dt_key: str = Header(default="")):
    """AI Video Generator: text prompt -> short MP4 (LTX-Video distilled
    on a public HF Space). Job pattern: returns {"job": id} immediately;
    poll /genjob/<id> for the file."""
    check_key(x_dt_key)
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "No prompt given.")
    prompt = prompt[:500]
    # Parameter order/defaults exactly as proven in the 2026-11-10 tests
    # (only the prompt is ours): prompt, negative prompt, -, -, height,
    # width, mode, duration s, -, seed, -, -, -.
    data = [prompt,
            "worst quality, inconsistent motion, blurry, jittery, distorted",
            None, None, 512, 704, "text-to-video", 2, 9, 42, True, 1, True]
    return _gen_start("video", LTX_SPACE, LTX_FN_INDEX, data, 240)


@app.post("/genmusic")
def genmusic(payload: dict, x_dt_key: str = Header(default="")):
    """AI Music Generator: style prompt -> MP3 (ACE-Step v1 on a public
    HF Space). Job pattern: returns {"job": id} immediately; poll
    /genjob/<id> for the file."""
    check_key(x_dt_key)
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "No prompt given.")
    prompt = prompt[:300]
    try:
        duration = int(payload.get("duration") or 20)
    except Exception:
        duration = 20
    duration = max(10, min(duration, 60))
    # Parameter order/defaults exactly as proven in the 2026-11-10 tests:
    # duration, tags (our prompt), lyrics [instrumental], steps, guidance,
    # scheduler, cfg type, omega, seeds, then the demo's fixed defaults.
    data = [duration, prompt, "[instrumental]", 30, 15.0, "euler", "apg",
            10.0, None, 0.5, 0.0, 3.0, True, False, True, None, 0.0, 0.0,
            False, 0.5, None, "none"]
    return _gen_start("music", ACE_SPACE, ACE_FN_INDEX, data, 180)


@app.post("/editimage")
def editimage(payload: dict, x_dt_key: str = Header(default="")):
    """AI Photo Editor: photo + edit instruction -> edited image
    (Qwen-Image-Edit on a public HF Space). Job pattern: returns
    {"job": id} immediately; poll /genjob/<id> for the file. The photo
    arrives base64-encoded (max ~4 MB decoded) and is written to a temp
    file the worker uploads to the Space, then deletes."""
    check_key(x_dt_key)
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "No prompt given.")
    prompt = prompt[:500]
    b64 = (payload.get("image_b64") or "").strip()
    if b64.startswith("data:") and "," in b64[:64]:
        b64 = b64.split(",", 1)[1]
    if not b64:
        raise HTTPException(400, "No image given.")
    if len(b64) > (EDIT_MAX_BYTES * 4 // 3) + 1024:
        raise HTTPException(413, "Image too large.")
    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception:
        raise HTTPException(400, "Image is not valid base64.")
    if not raw or len(raw) > EDIT_MAX_BYTES:
        raise HTTPException(413, "Image too large.")
    # Sniff the real type from the magic bytes (drives the upload's
    # filename/mime); anything else is rejected rather than guessed.
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        ext, mime = ".png", "image/png"
    elif raw[:3] == b"\xff\xd8\xff":
        ext, mime = ".jpg", "image/jpeg"
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        ext, mime = ".webp", "image/webp"
    else:
        raise HTTPException(
            400, "Unsupported image type (use PNG, JPEG or WebP).")
    tmp_path = os.path.join(GEN_DIR, "edit-" + _uuid.uuid4().hex[:12] + ext)
    with open(tmp_path, "wb") as fh:
        fh.write(raw)
    data = {"tmp_path": tmp_path, "prompt": prompt,
            "filename": "image" + ext, "mime": mime}
    try:
        return _gen_start("image", QWEN_SPACE, QWEN_FN_INDEX, data, 300,
                          worker=_edit_worker)
    except Exception:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        raise


@app.get("/genjob/{job_id}")
def genjob(job_id: str, x_dt_key: str = Header(default="")):
    """Poll an AI generation job: {"state": "working"} while it runs,
    {"state": "error", "detail": ...} on failure, or the finished file
    itself (200 + attachment) once done."""
    check_key(x_dt_key)
    with _gen_lock:
        _gen_housekeeping_locked()
        job = _gen_jobs.get(job_id)
        if not job:
            return JSONResponse(status_code=404,
                                content={"error": "unknown job"})
        state = job["state"]
        detail = job.get("detail")
        path, media, fname = job.get("path"), job.get("media"), job.get("filename")
    if state == "working":
        return {"state": "working"}
    if state == "error":
        return {"state": "error",
                "detail": detail or "The generation failed. Please try again."}
    try:
        with open(path, "rb") as fh:
            content = fh.read()
    except Exception:
        return JSONResponse(
            status_code=410,
            content={"state": "error",
                     "detail": "The generated file has expired. "
                               "Please generate again."})
    return Response(content=content, media_type=media, headers={
        "Content-Disposition": 'attachment; filename="' + fname + '"'})



# ---------------------------------------------------------------------------
# Visual Voice Creator (v109, user order 2026-11-20; spec locked 2026-11-13
# in hidden_files/ai-video-remaker-spec-2026-11-13.md).
#
# The user uploads their OWN video. The original visuals are discarded;
# only the user's own voice is kept — never replaced — and enhanced
# (sweetness/clarity + a "heavy" echo body) with one of three presets the
# user picks in the tool:
#   realistic  — clean presence lift, gentle compression, a touch of echo
#   dheema     — slower (atempo 0.92) and calmer: softer top, more echo
#   aesthetic  — warmer low end, wider two-tap echo
# Behind the voice runs a slideshow of 2-4 pictures splitting the total
# duration EQUALLY, each with a slow Ken Burns zoom. Pictures come from a
# bundled set (vvcimg/islamic, vvcimg/nature — AI-generated artwork, no
# people) or from the user's own uploads (POST /vvcimg with a token the
# site generates; POST /vvccap optionally leaves a captions SRT under the
# same token). Captions, when present, are burned in with libass using
# the bundled Noto fonts (vvcimg/fonts) so Arabic/Urdu shaping is right.
#
# POST /vvc is a job, like /genvideo: it returns {"job": id} at once and
# the finished MP4 is collected through the existing GET /genjob/<id>.
# The free engine is weak (0.25 CPU, hourly restarts), so inputs are
# capped at ~5 minutes and renders use fast presets; the worker also
# heartbeats the job's creation time at segment boundaries so the gen
# housekeeping TTL (15 min) cannot reap a long-but-healthy render.
# ---------------------------------------------------------------------------
VVC_DIR = "/tmp/dt-vvc"
VVC_WORK = "/tmp/dt-vvcwork"
VVCIMG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vvcimg")
VVC_MAX_BYTES = 60 * 1024 * 1024     # uploaded video cap
VVC_IMG_MAX = 8 * 1024 * 1024        # one uploaded picture cap
VVC_MAX_DUR = 320.0                  # seconds (~5 min + a little slack)
VVC_FPS = 24

VVC_VOICE_FILTERS = {
    # Sweetness = presence/clarity lift + a warm low-mid body; heavy =
    # a short echo under the voice; loudnorm evens the level out.
    "realistic": (
        "highpass=f=70,"
        "equalizer=f=280:t=q:w=1.2:g=2,"
        "equalizer=f=3200:t=q:w=1:g=3,"
        "acompressor=threshold=-18dB:ratio=3:attack=20:release=250,"
        "aecho=0.8:0.7:60:0.14,"
        "loudnorm=I=-15:TP=-1.5:LRA=11"),
    "dheema": (
        "atempo=0.92,"
        "highpass=f=70,"
        "equalizer=f=280:t=q:w=1.2:g=2,"
        "equalizer=f=3200:t=q:w=1:g=1.5,"
        "acompressor=threshold=-20dB:ratio=2.5:attack=30:release=300,"
        "aecho=0.8:0.75:90:0.2,"
        "loudnorm=I=-16:TP=-1.5:LRA=11"),
    "aesthetic": (
        "highpass=f=60,"
        "lowshelf=f=200:g=3,"
        "equalizer=f=3000:t=q:w=1:g=2,"
        "acompressor=threshold=-18dB:ratio=3:attack=20:release=250,"
        "aecho=0.85:0.8:110|55:0.24|0.12,"
        "loudnorm=I=-15:TP=-1.5:LRA=11"),
}


def _vvc_token_dir(token):
    """Uploads for one VVC session live under /tmp/dt-vvc/<token>/ —
    the token is a client-generated hex string; anything else is
    rejected so it can never escape the folder."""
    token = re.sub(r"[^a-f0-9]", "", (token or "").lower())[:32]
    if len(token) < 8:
        return None
    return os.path.join(VVC_DIR, token)


def _vvc_sniff_image(raw):
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if raw[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    return None


@app.post("/vvcimg")
async def vvcimg(request: Request, token: str = "",
                 x_dt_key: str = Header(default="")):
    """One of the user's own slideshow pictures for a VVC job (raw body,
    like /convert). Stored under the session token; /vvc picks them up
    in upload order. At most 4 pictures per session."""
    check_key(x_dt_key)
    tdir = _vvc_token_dir(token)
    if not tdir:
        raise HTTPException(400, "bad session token")
    data = await request.body()
    if not data:
        raise HTTPException(400, "empty upload")
    if len(data) > VVC_IMG_MAX:
        raise HTTPException(413, "image too large (8 MB max)")
    ext = _vvc_sniff_image(data)
    if not ext:
        raise HTTPException(400, "Unsupported image type (use PNG, JPEG or WebP).")
    os.makedirs(tdir, exist_ok=True)
    existing = sorted(f for f in os.listdir(tdir) if f.startswith("img"))
    if len(existing) >= 4:
        raise HTTPException(400, "too many pictures (4 max)")
    with open(os.path.join(tdir, "img%d%s" % (len(existing), ext)), "wb") as fh:
        fh.write(data)
    return {"ok": True, "count": len(existing) + 1}


@app.post("/vvccap")
def vvccap(payload: dict, x_dt_key: str = Header(default="")):
    """The captions SRT the site prepared for a VVC job (transcript +
    optional translation, evenly timed by the site), stored under the
    session token for /vvc to burn in."""
    check_key(x_dt_key)
    tdir = _vvc_token_dir(str(payload.get("token") or ""))
    if not tdir:
        raise HTTPException(400, "bad session token")
    srt = str(payload.get("srt") or "")
    if not srt.strip():
        raise HTTPException(400, "no captions given")
    if len(srt.encode("utf-8")) > 200 * 1024:
        raise HTTPException(413, "captions too large")
    os.makedirs(tdir, exist_ok=True)
    with open(os.path.join(tdir, "caps.srt"), "w", encoding="utf-8") as fh:
        fh.write(srt)
    return {"ok": True}


def _vvc_run(cmd, timeout, cwd=None):
    import subprocess
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                              cwd=cwd)
    except Exception as e:
        raise _GenFail("A rendering step could not start. Please try again.")
    if proc.returncode != 0:
        tail = (proc.stderr or b"").decode("utf-8", "replace")[-300:]
        raise _GenFail("Rendering failed: " + tail.strip()[-200:])


def _vvc_probe(path):
    """(duration_s, width, height, has_audio) via ffprobe; duration and
    dimensions fall back to 0 when the probe cannot read them."""
    import subprocess
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,width,height",
             "-of", "json", path],
            capture_output=True, timeout=30)
        info = _json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    except Exception:
        return 0.0, 0, 0, False
    dur = 0.0
    try:
        dur = float((info.get("format") or {}).get("duration") or 0)
    except Exception:
        dur = 0.0
    w = h = 0
    has_audio = False
    for st in info.get("streams") or []:
        if st.get("codec_type") == "audio":
            has_audio = True
        elif st.get("codec_type") == "video" and not w:
            w = int(st.get("width") or 0)
            h = int(st.get("height") or 0)
    return dur, w, h, has_audio


def _vvc_worker(job_id, src_path, voicestyle, visualset, token, npics):
    import shutil
    import subprocess  # noqa: F401  (kept local like /convert's imports)
    workdir = os.path.join(VVC_WORK, job_id)
    tdir = _vvc_token_dir(token) if token else None
    detail = "The video could not be created. Please try again."

    def heartbeat():
        with _gen_lock:
            job = _gen_jobs.get(job_id)
            if job:
                job["created"] = time.time()

    try:
        os.makedirs(workdir, exist_ok=True)
        dur, w, h, has_audio = _vvc_probe(src_path)
        if not has_audio:
            raise _GenFail(
                "This video has no sound, so there is no voice to keep. "
                "Please choose a video with the voice recorded in it.")
        if dur <= 0:
            raise _GenFail("This video file could not be read. "
                           "Please try another file.")
        if dur > VVC_MAX_DUR:
            raise _GenFail(
                "This video is longer than 5 minutes. Please choose a "
                "shorter video — the free engine cannot render long ones.")
        portrait = h >= w
        W, H = (720, 1280) if portrait else (1280, 720)

        # 1) The user's own voice, enhanced (never replaced).
        audio_path = os.path.join(workdir, "voice.m4a")
        _vvc_run(["ffmpeg", "-y", "-i", src_path, "-vn",
                  "-af", VVC_VOICE_FILTERS[voicestyle],
                  "-c:a", "aac", "-b:a", "128k", audio_path], 420)
        adur, _, _, _ = _vvc_probe(audio_path)
        if adur <= 0:
            adur = dur

        # 2) The pictures: the user's uploads, or a bundled set.
        if visualset == "photos":
            if not tdir or not os.path.isdir(tdir):
                raise _GenFail("Your photos could not be found. "
                               "Please upload them again.")
            imgs = [os.path.join(tdir, f) for f in sorted(os.listdir(tdir))
                    if f.startswith("img")]
            if len(imgs) < 2:
                raise _GenFail("Please upload at least 2 photos.")
            imgs = imgs[:4]
            npics = len(imgs)
        else:
            setdir = os.path.join(VVCIMG_DIR, visualset)
            pool = []
            if os.path.isdir(setdir):
                pool = [os.path.join(setdir, f)
                        for f in sorted(os.listdir(setdir))
                        if f.lower().endswith((".jpg", ".jpeg", ".png"))]
            if not pool:
                raise _GenFail("The picture set is unavailable right now. "
                               "Please try again later.")
            imgs = [pool[i % len(pool)] for i in range(npics)]

        # 3) One silent segment per picture, splitting the (enhanced)
        #    audio duration equally, each with a slow Ken Burns zoom —
        #    zooming in and out alternately so it never feels static.
        seg = adur / float(npics)
        frames = max(VVC_FPS, int(round(seg * VVC_FPS)))
        preset = "veryfast" if seg <= 60 else "ultrafast"
        listpath = os.path.join(workdir, "list.txt")
        with open(listpath, "w", encoding="utf-8") as lf:
            for i, img in enumerate(imgs):
                if i % 2 == 0:
                    z = "min(1.0+0.10*on/%d,1.10)" % frames
                else:
                    z = "max(1.10-0.10*on/%d,1.0)" % frames
                vf = (
                    "scale=%d:%d:force_original_aspect_ratio=increase,"
                    "crop=%d:%d,"
                    "zoompan=z='%s':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                    ":d=%d:s=%dx%d:fps=%d,format=yuv420p"
                    % (2 * W, 2 * H, 2 * W, 2 * H, z, frames, W, H, VVC_FPS))
                segpath = os.path.join(workdir, "seg%d.mp4" % i)
                _vvc_run(["ffmpeg", "-y", "-i", img, "-vf", vf,
                          "-frames:v", str(frames),
                          "-c:v", "libx264", "-preset", preset,
                          "-crf", "21", segpath], 900)
                lf.write("file 'seg%d.mp4'\n" % i)
                heartbeat()
        joined = os.path.join(workdir, "joined.mp4")
        _vvc_run(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                  "-i", listpath, "-c", "copy", joined], 300)

        # 4) Mux the voice back; burn captions in when the site sent any.
        final = os.path.join(workdir, "final.mp4")
        caps = None
        if tdir:
            cand = os.path.join(tdir, "caps.srt")
            if os.path.exists(cand):
                caps = os.path.join(workdir, "caps.srt")
                shutil.copyfile(cand, caps)
        if caps:
            style = ("FontName=Noto Sans Arabic,FontSize=16,"
                     "PrimaryColour=&HFFFFFF,BorderStyle=3,"
                     "BackColour=&H990B5563,Outline=6,Shadow=0,"
                     "Alignment=2,MarginV=28")
            vf = ("subtitles=caps.srt:fontsdir="
                  + os.path.join(VVCIMG_DIR, "fonts")
                  + ":force_style='" + style + "'")
            _vvc_run(["ffmpeg", "-y", "-i", joined, "-i", audio_path,
                      "-vf", vf, "-c:v", "libx264", "-preset", "veryfast",
                      "-crf", "21", "-c:a", "aac", "-b:a", "128k",
                      "-shortest", "-movflags", "+faststart", final],
                     1200, cwd=workdir)
        else:
            _vvc_run(["ffmpeg", "-y", "-i", joined, "-i", audio_path,
                      "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                      "-shortest", "-movflags", "+faststart", final], 600)

        dest = os.path.join(GEN_DIR, job_id + ".mp4")
        shutil.move(final, dest)
        with _gen_lock:
            job = _gen_jobs.get(job_id)
            if job:
                job.update(state="done", path=dest, media="video/mp4",
                           filename="dahot-visual-voice.mp4")
    except _GenFail as e:
        detail = e.detail
        with _gen_lock:
            job = _gen_jobs.get(job_id)
            if job:
                job.update(state="error", detail=detail)
    except Exception:
        with _gen_lock:
            job = _gen_jobs.get(job_id)
            if job:
                job.update(state="error", detail=detail)
    finally:
        import shutil as _sh
        _sh.rmtree(workdir, ignore_errors=True)
        try:
            os.remove(src_path)
        except Exception:
            pass
        if tdir:
            _sh.rmtree(tdir, ignore_errors=True)


@app.post("/vvc")
async def vvc(request: Request, voicestyle: str = "realistic",
              visualset: str = "islamic", token: str = "", npics: int = 3,
              name: str = "video", x_dt_key: str = Header(default="")):
    """Visual Voice Creator: the user's video (raw body, like /convert)
    + options in the query string -> {"job": id}; poll /genjob/<id> for
    the finished MP4. For visualset="photos" the pictures (and optional
    captions) must already sit under the session token (see /vvcimg and
    /vvccap)."""
    check_key(x_dt_key)
    voicestyle = (voicestyle or "realistic").lower()
    if voicestyle not in VVC_VOICE_FILTERS:
        voicestyle = "realistic"
    visualset = (visualset or "islamic").lower()
    if visualset not in ("islamic", "nature", "photos"):
        raise HTTPException(400, "bad visual style")
    try:
        npics = int(npics or 3)
    except Exception:
        npics = 3
    npics = max(2, min(npics, 4))
    if visualset == "photos":
        tdir = _vvc_token_dir(token)
        if not tdir or not os.path.isdir(tdir):
            raise HTTPException(400, "photos missing — upload them first")
        have = [f for f in os.listdir(tdir) if f.startswith("img")]
        if len(have) < 2:
            raise HTTPException(400, "upload at least 2 photos first")
    data = await request.body()
    if not data:
        raise HTTPException(400, "empty upload")
    if len(data) > VVC_MAX_BYTES:
        raise HTTPException(413, "file too large (60 MB max)")
    with _gen_lock:
        _gen_housekeeping_locked()
        active = sum(1 for j in _gen_jobs.values() if j["state"] == "working")
        if active >= GEN_MAX_ACTIVE:
            raise HTTPException(
                503, "The creation service is busy right now — "
                     "please try again in a moment.")
        job_id = _uuid.uuid4().hex[:16]
        _gen_jobs[job_id] = {"id": job_id, "kind": "video",
                             "state": "working", "created": time.time(),
                             "path": None, "media": None, "filename": None,
                             "detail": None}
    src_path = os.path.join(GEN_DIR, "vvcin-" + job_id + ".bin")
    with open(src_path, "wb") as fh:
        fh.write(data)
    _threading.Thread(target=_vvc_worker,
                      args=(job_id, src_path, voicestyle, visualset,
                            token, npics),
                      daemon=True).start()
    return {"job": job_id}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
