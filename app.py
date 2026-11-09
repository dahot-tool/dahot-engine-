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
import time
import urllib.parse
import urllib.request

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


@app.get("/fetch")
def fetch(u: str, f: str = "", x_dt_key: str = Header(default="")):
    check_key(x_dt_key)
    if not re.match(r"^https?://", u or ""):
        raise HTTPException(status_code=400, detail="bad url")
    if is_terabox_url(u):
        # Re-resolve the share and stream the fresh dlink (see above).
        return fetch_terabox(u, f)
    fmt = f or "bv*[height<=720]+ba/b[height<=720]/b"
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
            return path, (info.get("title") or "video")

    try:
        if is_youtube_url(u):
            # Download through the same client cycle as /resolve: the
            # client that can see the real formats is the one that can
            # fetch them.
            (path, title), _label = cycle_youtube(
                lambda pc, imp: _download(
                    ydl_opts(extra, player_client=pc, impersonate=imp)))
        else:
            path, title = _download(ydl_opts(extra))
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=422, detail=str(e)[:300])
    if not os.path.exists(path):
        raise HTTPException(status_code=500, detail="file missing after download")

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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
