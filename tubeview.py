#!/usr/bin/env python3
"""TubeView: a Nuvio-style YouTube front end. yt-dlp fetches data, mpv plays, the browser is the UI.
Sign-in reuses the YouTube login of the user's default web browser."""
import base64, hashlib, json, os, re, secrets, shutil, socket, subprocess, sys, threading, time, urllib.error, urllib.request, webbrowser
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import unquote, urlencode, urljoin, urlparse, parse_qs

import yt_dlp

FROZEN = getattr(sys, "frozen", False)  # the packaged app (PyInstaller): Windows .exe / Linux AppImage
WINDOWS = os.name == "nt"
HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
# packaged builds carry their own ffmpeg and deno (yt-dlp's JavaScript runtime) in bin/
if (HERE / "bin").is_dir():
    os.environ["PATH"] = str(HERE / "bin") + os.pathsep + os.environ.get("PATH", "")
# the packaged app's OpenSSL looks for certificates where the build machine (Ubuntu) keeps them, which other
# distros don't, so every HTTPS request failed there; use the certificate list it carries instead
if FROZEN and not os.environ.get("SSL_CERT_FILE"):
    try:
        import certifi
        os.environ["SSL_CERT_FILE"] = certifi.where()
    except ImportError:
        pass
PORT = 8765
HOSTS = (f"127.0.0.1:{PORT}", f"localhost:{PORT}")
# The Android app runs this same server inside the app and points these at its own folders
PLATFORM = os.environ.get("TUBEVIEW_PLATFORM") or sys.platform
CONF_DIR = Path(os.environ.get("TUBEVIEW_CONF") or (
    Path(os.environ.get("APPDATA") or Path.home()) / "TubeView" if WINDOWS
    else Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "tubeview"))
WEB_DIR = Path(os.environ.get("TUBEVIEW_WEB") or HERE)  # index.html, remote.html
CONFIG = CONF_DIR / "config.json"
# hide the console window Windows would otherwise flash up for each ffmpeg/mpv run
NO_WINDOW = {"creationflags": 0x08000000} if WINDOWS else {}
JS_RUNTIME = "deno" if shutil.which("deno") else "node" if shutil.which("node") else None


def ytdl_path():
    """A yt-dlp executable for mpv: the venv's, or in the packaged Linux app a wrapper that runs the app as yt-dlp."""
    venv = shutil.which("yt-dlp", path=str(Path(sys.executable).parent))
    if venv and not FROZEN:
        return venv
    if FROZEN and not WINDOWS:
        wrapper = CONF_DIR / "yt-dlp"
        CONF_DIR.mkdir(parents=True, exist_ok=True)
        # rewritten every start: an AppImage is mounted at a new path each run
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" --yt-dlp "$@"\n')
        wrapper.chmod(0o755)
        return str(wrapper)
    return shutil.which("yt-dlp") or "yt-dlp"


YTDL = ytdl_path()
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
PLAYLIST_ID = re.compile(r"^(PL|OL|UU|LL|FL|RD|WL)[A-Za-z0-9_-]{0,62}$")
LANG = re.compile(r"^[A-Za-z0-9-]{1,20}$")
LIBRARY = {"history": ":ythis", "watchlater": ":ytwatchlater", "liked": ":ytfav"}
CHANNEL_TABS = {"videos", "shorts", "streams", "playlists"}

H, FP = Path.home(), Path.home() / ".var/app"
# keyword in the default browser's .desktop entry -> (display name, yt-dlp browser, profile roots: native, flatpak)
BROWSERS = [
    ("waterfox", "Waterfox", "firefox", [H / ".waterfox", FP / "net.waterfox.waterfox/.waterfox"]),
    ("librewolf", "LibreWolf", "firefox", [H / ".librewolf", FP / "io.gitlab.librewolf-community/.librewolf"]),
    ("zen", "Zen", "firefox", [H / ".zen", FP / "app.zen_browser.zen/.zen"]),
    ("floorp", "Floorp", "firefox", [H / ".floorp", FP / "one.ablaze.floorp/.floorp"]),
    ("firefox", "Firefox", "firefox", [H / ".mozilla/firefox", H / ".config/mozilla/firefox",
                                       FP / "org.mozilla.firefox/.mozilla/firefox"]),
    ("brave", "Brave", "brave", [H / ".config/BraveSoftware/Brave-Browser",
                                 FP / "com.brave.Browser/config/BraveSoftware/Brave-Browser"]),
    ("vivaldi", "Vivaldi", "vivaldi", [H / ".config/vivaldi", FP / "com.vivaldi.Vivaldi/config/vivaldi"]),
    ("edge", "Edge", "edge", [H / ".config/microsoft-edge", FP / "com.microsoft.Edge/config/microsoft-edge"]),
    ("opera", "Opera", "opera", [H / ".config/opera", FP / "com.opera.Opera/config/opera"]),
    ("ungoogled", "Ungoogled Chromium", "chromium",
     [FP / "io.github.ungoogled_software.ungoogled_chromium/config/chromium", H / ".config/chromium"]),
    ("chromium", "Chromium", "chromium", [H / ".config/chromium", FP / "org.chromium.Chromium/config/chromium"]),
    ("chrome", "Chrome", "chrome", [H / ".config/google-chrome", FP / "com.google.Chrome/config/google-chrome"]),
]
DESKTOP_DIRS = [H / ".local/share/applications", H / ".local/share/flatpak/exports/share/applications",
                Path("/var/lib/flatpak/exports/share/applications"), Path("/usr/share/applications")]

try:
    cfg = json.loads(CONFIG.read_text())
except (OSError, ValueError):
    cfg = {}


# Windows: default browser's ProgId keyword -> (name, yt-dlp browser, profile folder under %APPDATA% or None)
WIN_BROWSERS = [("waterfox", "Waterfox", "firefox", "Waterfox/Profiles"), ("librewolf", "LibreWolf", "firefox", "librewolf/Profiles"),
                ("floorp", "Floorp", "firefox", "Floorp/Profiles"), ("zen", "Zen", "firefox", "zen/Profiles"),
                ("firefox", "Firefox", "firefox", None), ("brave", "Brave", "brave", None), ("msedge", "Edge", "edge", None),
                ("chrome", "Chrome", "chrome", None), ("opera", "Opera", "opera", None), ("vivaldi", "Vivaldi", "vivaldi", None)]


def default_browser_windows():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice") as k:
            prog = winreg.QueryValueEx(k, "ProgId")[0].lower()
    except OSError:
        return None
    for key, name, browser, profile in WIN_BROWSERS:
        if key in prog:
            root = Path(os.environ.get("APPDATA", "")) / profile if profile else None
            return {"name": name, "browser": browser, "profile": str(root) if root and root.is_dir() else None}
    return None


def default_browser():
    """-> {"name", "browser", "profile"} for the user's default browser, or None if unknown."""
    if WINDOWS:
        return default_browser_windows()
    try:
        desktop = subprocess.run(["xdg-settings", "get", "default-web-browser"],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    # the .desktop id is usually enough; fall back to its Name=/Exec= lines (e.g. userapp-*.desktop)
    entry = next((d / desktop for d in DESKTOP_DIRS if desktop and (d / desktop).is_file()), None)
    lines = entry.read_text(errors="ignore").splitlines() if entry else []
    for text in (desktop.lower(), " ".join(l for l in lines if l.startswith(("Name=", "Exec="))).lower()):
        for key, name, browser, roots in BROWSERS:
            if key in text:
                profile = next((r for r in roots if r.is_dir()), None)
                return {"name": name, "browser": browser, "profile": str(profile) if profile else None}
    return None


def ydl(spec=None, **opts):
    spec = spec or cfg
    if spec:
        # yt-dlp picks the most recently used profile under this folder
        opts["cookiesfrombrowser"] = (spec["browser"], spec.get("profile"), None, None)
    # a JS runtime solves YouTube's playback challenges; without it signed-in requests get no video formats
    return yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "no_color": True, "skip_download": True,
                             "js_runtimes": {JS_RUNTIME: {"path": None}} if JS_RUNTIME else {}, **opts})


def slim(e):
    return {
        "id": e["id"],
        "title": e.get("title") or "",
        "channel": e.get("channel") or e.get("uploader") or "",
        "duration": e.get("duration"),
        "views": e.get("view_count"),
        "channel_id": e.get("channel_id"),
        "live": e.get("live_status") if e.get("live_status") in ("is_live", "is_upcoming") else None,
    }


def videos(info):
    return [slim(e) for e in info.get("entries") or [] if VIDEO_ID.match(e.get("id") or "")]


def feed(url, n=48, spec=None):
    with ydl(spec, extract_flat="in_playlist", playlistend=n) as y:
        return y.extract_info(url, download=False)


# --- Home page data, kept on disk so the app opens straight onto the last results (a TV takes seconds per fetch) ---
HOME_CACHE = CONFIG.parent / "home-cache.json"
home_lock = threading.Lock()
home = {"data": None, "refreshing": set()}


def home_data():
    if home["data"] is None:
        try:
            home["data"] = json.loads(HOME_CACHE.read_text())
        except (OSError, ValueError):
            home["data"] = {}
    return home["data"]


def save_home(data):
    # the 60 most recent entries
    home["data"] = data = dict(sorted(data.items(), key=lambda kv: kv[1][0])[-60:])
    HOME_CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = HOME_CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(HOME_CACHE)


def keep(key, value):
    with home_lock:
        save_home({**home_data(), key: [time.time(), value]})
    return value


def kept(key, ttl, fn, valid=lambda v: True):
    """fn()'s result, cached on disk. Once it's older than ttl (or no longer valid) the old result is
    still returned at once, and a background thread replaces it for next time."""
    with home_lock:
        hit = home_data().get(key)
    if not hit:
        value = fn()
        return keep(key, value) if value else value
    if time.time() - hit[0] > ttl or not valid(hit[1]):
        with home_lock:
            busy = key in home["refreshing"]
            home["refreshing"].add(key)
        if not busy:
            def refresh():
                try:
                    value = fn()
                    if value:
                        keep(key, value)
                except Exception:
                    pass  # keep showing the old result
                finally:
                    home["refreshing"].discard(key)
            threading.Thread(target=refresh, daemon=True).start()
    return hit[1]


def forget(*prefixes):
    """Drop cached results whose key starts with any of these ("" drops everything)."""
    with home_lock:
        save_home({k: v for k, v in home_data().items() if not k.startswith(prefixes)})


def search(q):
    return kept("search:" + q, 3 * 3600, lambda: videos(feed(f"ytsearch24:{q}")))


def mix(v):
    """YouTube's endless mix for a video: what YouTube itself plays after it."""
    try:
        return [x for x in videos(feed(f"https://www.youtube.com/watch?v={v['id']}&list=RD{v['id']}", 25)) if x["id"] != v["id"]]
    except Exception:
        return []


def for_you():
    """Recommendations without a YouTube sign-in: the mixes of what you watched recently, interleaved."""
    seeds = [v for v in read_data().get("history") or [] if isinstance(v, dict) and VIDEO_ID.match(v.get("id") or "")][:6]
    seeds = list({v["id"]: v for v in seeds}.values())[:8]
    ids = [v["id"] for v in seeds]
    if not seeds:
        return {"seeds": [], "picks": [], "because": []}

    def build():
        with ThreadPoolExecutor(4) as ex:
            per = list(ex.map(mix, seeds))
        seen, picks = set(ids), []
        for rnd in range(25):
            for vs in per:
                if len(vs) > rnd and vs[rnd]["id"] not in seen:
                    seen.add(vs[rnd]["id"])
                    picks.append(vs[rnd])
        because = [{"seed": {"id": s["id"], "title": s.get("title") or ""}, "videos": vs}
                   for s, vs in zip(seeds, per) if len(vs) >= 6][:3]
        return {"seeds": ids, "picks": picks[:96], "because": because} if picks else None
    return kept("foryou", 2 * 3600, build, lambda v: v.get("seeds") == ids) or {"seeds": ids, "picks": [], "because": []}


@lru_cache(maxsize=128)
def video(vid):
    # the player's own lookup: opening a video's details also gets its streams ready, so Play starts sooner
    try:
        e = play_info(vid)
    except Exception:  # e.g. an upcoming premiere, which has no streams yet
        with ydl() as y:
            e = y.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False, process=False)
    return {**slim(e), "description": e.get("description") or "", "likes": e.get("like_count"),
            "date": e.get("upload_date"), "channel_id": e.get("channel_id"),
            "track": e.get("track"), "artists": e.get("artists") or [], "album": e.get("album")}


# --- Music: songs come from YouTube Music; "radio" is YouTube's endless mix for a song ---

def artist_name(name):
    return (name or "").removesuffix(" - Topic")


def song(e):
    return {"id": e["id"], "title": e.get("track") or e.get("title") or "",
            "artist": ", ".join(e.get("artists") or []) or artist_name(e.get("channel") or e.get("uploader")),
            "album": e.get("album") or "", "duration": e.get("duration"), "channel_id": e.get("channel_id")}


def songs(info):
    return [song(e) for e in info.get("entries") or [] if VIDEO_ID.match(e.get("id") or "")]


# rows on the Music page: [title, search]; prefetched at startup so the page opens instantly
MUSIC_ROWS = [["Top Hits", "top hits 2026"], ["New Releases", "new music this week"], ["Chill", "chill vibes"],
              ["Hip-Hop", "hip hop hits"], ["Rock Classics", "rock classics"], ["Dance & Electronic", "dance electronic hits"]]
MUSIC_CACHE = CONFIG.parent / "music-cache.json"
MUSIC_TTL = 6 * 3600
music_lock = threading.Lock()


def music_search(q):
    """Song search, cached on disk for 6 hours (a fresh search takes a few seconds)."""
    with music_lock:
        try:
            cache = json.loads(MUSIC_CACHE.read_text())
        except (OSError, ValueError):
            cache = {}
    hit = cache.get(q)
    if hit and time.time() - hit[0] < MUSIC_TTL:
        return hit[1]
    ids = [e["id"] for e in feed("https://music.youtube.com/search?" + urlencode({"q": q}) + "#songs", 25).get("entries") or []
           if VIDEO_ID.match(e.get("id") or "")]
    found = song_details(ids)
    with music_lock:
        try:
            cache = json.loads(MUSIC_CACHE.read_text())
        except (OSError, ValueError):
            cache = {}
        cache[q] = [time.time(), found]
        # keep the 100 most recent searches
        cache = dict(sorted(cache.items(), key=lambda kv: kv[1][0])[-100:])
        MUSIC_CACHE.parent.mkdir(parents=True, exist_ok=True)
        MUSIC_CACHE.write_text(json.dumps(cache))
    return found


def prefetch_music():
    for _, q in MUSIC_ROWS:
        try:
            music_search(q)
        except Exception:
            pass  # the page will just fetch it itself


def song_details(ids):
    """YouTube Music search lists only ids and titles; a temporary playlist of those ids
    returns artist and length for all of them in one request."""
    if not ids:
        return []
    found = {s["id"]: s for s in songs(feed("https://www.youtube.com/watch_videos?video_ids=" + ",".join(ids[:50]), 50))}
    return [found[i] for i in ids if i in found]


def radio(vid):
    return [s for s in songs(feed(f"https://www.youtube.com/watch?v={vid}&list=RDAMVM{vid}", 50)) if s["id"] != vid]


def music_for_you():
    """The Music page's own picks: YouTube Music's radio for the songs you played lately (a few different artists),
    interleaved, then rows for those songs and for the artists you play most."""
    hist = [x for x in read_data().get("musicHistory") or [] if isinstance(x, dict) and VIDEO_ID.match(x.get("id") or "")]
    seeds, artists = [], set()
    for x in hist:
        if norm(x.get("artist")) not in artists:
            artists.add(norm(x.get("artist")))
            seeds.append(x)
        if len(seeds) == 5:
            break
    if len(seeds) < 3 and ytv_session():  # little played here yet: your liked songs too
        try:
            seeds += [x for x in tv_liked_music()[:6] if x["id"] not in {y["id"] for y in seeds}][:3]
        except (OSError, AuthError):
            pass
    ids = [x["id"] for x in seeds]
    if not seeds:
        return {"seeds": [], "mix": [], "because": [], "artists": []}
    plays = {}
    for x in hist:
        plays[x.get("artist") or ""] = plays.get(x.get("artist") or "", 0) + 1
    top = [a for a, n in sorted(plays.items(), key=lambda kv: -kv[1]) if a and n >= 2][:2]

    def build():
        def safe(fn, arg):
            try:
                return fn(arg)
            except Exception:
                return []
        with ThreadPoolExecutor(4) as ex:
            radios = list(ex.map(lambda x: safe(radio, x["id"]), seeds))
            by_artist = list(ex.map(lambda a: safe(music_search, a), top))
        recent = {x["id"] for x in hist[:15]} | set(ids)
        mix, seen = [], set(recent)
        for rnd in range(25):
            for r in radios:
                if len(r) > rnd and r[rnd]["id"] not in seen:
                    seen.add(r[rnd]["id"])
                    mix.append(r[rnd])
        because = [{"seed": {"id": x["id"], "title": x.get("title") or ""}, "songs": r[:20]} for x, r in zip(seeds, radios) if len(r) >= 6][:3]
        return {"seeds": ids, "mix": mix[:50], "because": because,
                "artists": [{"name": artist_name(a), "songs": r[:20]} for a, r in zip(top, by_artist) if r]} if mix else None
    return kept("musicforyou", 3 * 3600, build, lambda v: v.get("seeds") == ids) or {"seeds": ids, "mix": [], "because": [], "artists": []}


# --- Lyrics from several services, the way Prism (github.com/Yigder/Prism) finds them: each service gets a head start
# to answer before the next one is asked as well, the answers are taken in order, and word-by-word (karaoke) timing
# beats line timing, which beats plain text. Lines are {"t": start ms (-1 untimed), "e": end ms, "text",
# "w": [[start ms, end ms, text], ...] for word-timed lines}. ---
LYRICS_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
LENGTH_GAP = 8  # seconds an edition may differ from the song playing (another edit's lyrics run early or late)
# credits Chinese services put before the lyrics ("作词 : …", "Keyboard：…"): a role, then a colon (the full-width one
# is only ever used there)
CREDIT_LINE = re.compile(r"^\s*([^:：]{1,30}：|.*(作词|作曲|编曲|制作人|制作|监制|混音|母带|录音|和声|吉他|贝斯|鼓|弦乐|出品|发行)\s*[:：]|"
                         r"(lyrics?|words|composed|composer|written|writer|produced|producer|arranged|music|mixed|mastered|"
                         r"vocals?|keyboards?|guitars?|bass|drums|piano|strings|programming|engineer(ed)?|recorded|"
                         r"executive producer|publisher|label|op|sp)\s*(by)?\s*:)", re.I)


def ly_get(url, params=None, headers=None, as_json=True, timeout=8):
    req = urllib.request.Request(url + ("?" + urlencode(params) if params else ""), headers={"User-Agent": LYRICS_UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "replace")
    return json.loads(body) if as_json else body


def clean_title(t):
    t = re.sub(r"\s*[(\[](official|lyric|lyrics|audio|video|visualizer|music video|mv|hd|4k|explicit)[^)\]]*[)\]]", "", t, flags=re.I)
    return re.sub(r"\s*-\s*(official|lyric).*$", "", t, flags=re.I).strip()


def norm(s):
    import unicodedata
    s = unicodedata.normalize("NFD", (s or "").lower())
    return "".join(c for c in s if c.isalnum() and not unicodedata.combining(c))


def fits(song, sec):
    return not song["duration"] or not sec or abs(sec - song["duration"]) <= LENGTH_GAP


def kept_line(out, start, text):
    """Credits ("作词 : …") and the "Title - Artist" card some services open with aren't lyrics."""
    return not (CREDIT_LINE.match(text) or (not out and start < 1000 and " - " in text))


def words_line(start, end, tokens):
    """Syllables [(start, dur, text)] glued into words (a space ends a word) and the line they make."""
    words, cur, ws, we = [], "", 0, 0
    for t, d, text in tokens:
        if not text:
            continue
        if text[0].isspace() and cur:
            words.append([ws, we, cur + " "])
            cur = ""
        if text.strip():
            if not cur:
                ws = t
            cur += text.strip()
            we = t + d
        if text[-1].isspace() and cur:
            words.append([ws, we, cur + " "])
            cur = ""
    if cur:
        words.append([ws, we, cur])
    text = html_unescape("".join(w[2] for w in words)).strip()
    return {"t": start, "e": end, "text": text, "w": [[a, b, html_unescape(c)] for a, b, c in words]} if text else None


def html_unescape(s):
    import html
    return html.unescape(s)


LRC_STAMP = re.compile(r"\[(\d+):(\d+(?:[.:]\d+)?)\]")


def parse_lrc(text):
    out = []
    for raw in text.splitlines():
        stamps = LRC_STAMP.findall(raw)
        body = LRC_STAMP.sub("", raw).strip()
        if not stamps or body.startswith("{"):
            continue
        for m, s in stamps:
            t = int((int(m) * 60 + float(s.replace(":", "."))) * 1000)
            if kept_line(out, t, body):
                out.append({"t": t, "e": None, "text": body})
    out.sort(key=lambda l: l["t"])
    for a, b in zip(out, out[1:]):
        a["e"] = b["t"]
    return out


def parse_words(kind, body):
    """Karaoke formats: KRC (KuGou) `[start,dur]<offset,dur,0>word…`, YRC (NetEase) `[start,dur](start,dur,0)word…`."""
    tag = re.compile(r"<(\d+),(\d+),-?\d+>" if kind == "krc" else r"\((\d+),(\d+),-?\d+\)")
    out = []
    for raw in body.splitlines():
        head = re.match(r"^\[(\d+),(\d+)\]", raw.strip())
        if not head:
            continue
        start, dur, rest = int(head[1]), int(head[2]), raw.strip()[head.end():]
        tags = list(tag.finditer(rest))
        tokens = [(int(m[1]) + (start if kind == "krc" else 0), int(m[2]),
                   rest[m.end():tags[i + 1].start() if i + 1 < len(tags) else len(rest)]) for i, m in enumerate(tags)]
        line = words_line(start, start + dur, tokens)
        if line and kept_line(out, start, line["text"]):
            out.append(line)
    return out


def ttml_time(t):
    try:
        return int(sum(float(p) * 60 ** i for i, p in enumerate(reversed(t.strip().removesuffix("s").split(":")))) * 1000)
    except (AttributeError, ValueError):
        return None


def parse_ttml(ttml):
    """Apple Music's TTML: one <p> per line, one <span> per word when word-timed. Background vocals are left out."""
    out = []
    attr = lambda a, n: (re.search(rf'\b{n}="([^"]*)"', a) or [None, None])[1]
    for m in re.finditer(r"<p\b([^>]*)>(.*?)</p>", ttml, re.S):
        begin, end = ttml_time(attr(m[1], "begin")), ttml_time(attr(m[1], "end"))
        content = m[2]
        while (bg := re.search(r'<span[^>]*ttm:role="x-bg"[^>]*>', content)):  # nested spans: cut to the matching close
            depth, cut = 0, None
            for t in re.finditer(r"<span\b[^>]*>|</span>", content[bg.start():]):
                depth += -1 if t[0].startswith("</") else 1
                if depth == 0:
                    cut = bg.start() + t.end()
                    break
            content = content[:bg.start()] + (content[cut:] if cut else "")
        spans = list(re.finditer(r"<span\b([^>]*)>([^<]*)</span>", content))
        timed = [(ttml_time(attr(s[1], "begin")), ttml_time(attr(s[1], "end")), s) for s in spans]
        if begin is None:
            text = html_unescape(re.sub(r"<[^>]+>", "", content)).strip()
            if text:
                out.append({"t": -1, "e": None, "text": text})
            continue
        if timed and all(b is not None for b, _, _ in timed):
            tokens, last = [], 0
            for b, e, s in timed:
                space = " " if re.search(r"\s", content[last:s.start()]) and tokens else ""
                tokens.append((b, (e or b) - b, space + s[2]))
                last = s.end()
            line = words_line(begin, end or begin, tokens)
        else:
            text = html_unescape(re.sub(r"<[^>]+>", "", content)).strip()
            line = {"t": begin, "e": end, "text": text} if text else None
        if line:
            out.append(line)
    return out


def parse_lyricsplus(d):
    if not isinstance(d, dict) or d.get("type") == "None":
        return []
    out = []
    for l in d.get("lyrics") or []:
        start, dur = int(l.get("time") or 0), int(l.get("duration") or 0)
        syl = [s for s in l.get("syllabus") or [] if not s.get("isBackground")]
        tokens = [(int(s.get("time") or 0), int(s.get("duration") or 0), s.get("text") or "") for s in syl]
        line = words_line(start, start + dur, tokens) if tokens else (
            {"t": start if d.get("type") != "None" else -1, "e": start + dur, "text": (l.get("text") or "").strip()} if (l.get("text") or "").strip() else None)
        if line:
            out.append(line)
    return out


def decode_krc(b64):
    import zlib
    data = base64.b64decode(b64)
    if data[:4] != b"krc1":
        return None
    key = [64, 71, 97, 119, 94, 50, 116, 71, 81, 54, 49, 45, 206, 210, 110, 105]
    return zlib.decompress(bytes(b ^ key[i % 16] for i, b in enumerate(data[4:]))).decode("utf-8", "replace")


def from_kugou(song):
    cands = []
    for kw in (f"{song['artist']} - {song['title']}", f"{song['title']} - {song['artist']}"):
        cands = ly_get("https://lyrics.kugou.com/search", {"ver": 1, "man": "yes", "client": "pc", "keyword": kw,
                                                            "duration": song["duration"] * 1000, "hash": ""}).get("candidates") or []
        if cands:
            break
    cands = sorted((c for c in cands if fits(song, (c.get("duration") or 0) / 1000)),
                   key=lambda c: abs((c.get("duration") or 0) - song["duration"] * 1000))
    for c in cands[:4]:
        dl = lambda fmt: ly_get("https://lyrics.kugou.com/download", {"fmt": fmt, "charset": "utf8", "client": "pc", "ver": 1,
                                                                       "id": c["id"], "accesskey": c["accesskey"]}).get("content")
        krc = decode_krc(dl("krc") or "")
        # KuGou files some edits under the album's length; the lyrics' own title says which edition they are
        title = re.search(r"\[ti:(.*?)\]", krc or "")
        if title and norm(clean_title(title[1])) not in (norm(song["title"]), "") and not norm(title[1]).startswith(norm(song["title"])):
            continue
        lines = parse_words("krc", krc) if krc else []
        if lines:
            return lines
        lrc = base64.b64decode(dl("lrc") or "").decode("utf-8", "replace")
        if parse_lrc(lrc):
            return parse_lrc(lrc)
    return None


def betterlyrics_params(song):
    return {"s": song["title"], "a": song["artist"], **({"d": song["duration"]} if song["duration"] else {}),
            **({"al": song["album"]} if song["album"] else {})}


def from_apple(song):
    """Apple Music's own word-timed lyrics, through the Better Lyrics API."""
    for base in ("https://api.betterlyrics.org/getLyrics", "https://lyrics-api.boidu.dev/getLyrics"):
        try:
            ttml = ly_get(base, betterlyrics_params(song)).get("ttml") or ""
        except (OSError, ValueError):
            continue
        if "<p" in ttml:
            return parse_ttml(ttml)
    return None


LYRICSPLUS = ["https://lyricsplus.binimum.org", "https://lyricsplus.prjktla.workers.dev", "https://lyricsplus.prjktla.my.id",
              "https://lyricsplus.atomix.one", "https://lyricsplus-seven.vercel.app", "https://lyrics-plus-backend.vercel.app"]
lyricsplus_good = []  # the mirror that answered last


def from_lyricsplus(song):
    """Syllable-timed lyrics (Apple, QQ, Musixmatch) from LyricsPlus, across volunteer mirrors."""
    params = {"title": song["title"], "artist": song["artist"], **({"duration": song["duration"]} if song["duration"] else {}),
              **({"album": song["album"]} if song["album"] else {})}
    for host in lyricsplus_good + [h for h in LYRICSPLUS if h not in lyricsplus_good]:
        try:
            lines = parse_lyricsplus(ly_get(host + "/v2/lyrics/get", params, timeout=4))
        except urllib.error.HTTPError as e:
            if e.code == 404 and lyricsplus_good[:1] == [host]:
                return None  # the mirror that works hasn't got it: the others won't either
            continue
        except (OSError, ValueError):
            continue
        if lines:
            lyricsplus_good[:] = [host]
            return lines
    return None


def from_netease(song):
    ref = {"Referer": "https://music.163.com/"}
    songs = ((ly_get("https://music.163.com/api/search/get", {"s": f"{song['title']} {song['artist']}", "type": 1, "limit": 10},
                     ref).get("result") or {}).get("songs") or [])
    want_t, want_a = norm(song["title"]), norm(song["artist"])
    cands = sorted((s for s in songs if (lambda t: t == want_t or want_t in t or t in want_t)(norm(s.get("name")))
                    and any(norm(a.get("name")) and (want_a in norm(a.get("name")) or norm(a.get("name")) in want_a) for a in s.get("artists") or [])
                    and fits(song, (s.get("duration") or 0) / 1000)),
                   key=lambda s: abs((s.get("duration") or 0) / 1000 - song["duration"]))
    for s in cands[:4]:
        r = ly_get("https://music.163.com/api/song/lyric", {"id": s["id"], "lv": 1, "kv": 1, "yv": 1, "tv": -1}, ref)
        yrc = (r.get("yrc") or {}).get("lyric") or ""
        if re.search(r"\[\d+,\d+\]\(", yrc) and (lines := parse_words("yrc", yrc)):
            return lines
        if (lines := parse_lrc((r.get("lrc") or {}).get("lyric") or "")):
            return lines
    return None


def from_lrclib(song):
    params = {"track_name": song["title"], "artist_name": song["artist"], **({"album_name": song["album"]} if song["album"] else {}),
              **({"duration": song["duration"]} if song["duration"] else {})}
    try:
        d = ly_get("https://lrclib.net/api/get", params)
    except urllib.error.HTTPError:
        hits = ly_get("https://lrclib.net/api/search", {"track_name": song["title"], "artist_name": song["artist"]}) or []
        hits = sorted((h for h in hits if fits(song, h.get("duration"))), key=lambda h: abs((h.get("duration") or 0) - song["duration"]))
        d = hits[0] if hits else {}
    if d and not fits(song, d.get("duration")):
        return None
    if d.get("instrumental"):
        return [{"t": -1, "e": None, "text": "", "instrumental": True}]
    if d.get("syncedLyrics") and len(lines := parse_lrc(d["syncedLyrics"])) >= 4:  # a few entries are placeholders
        return lines
    return [{"t": -1, "e": None, "text": l} for l in (d.get("plainLyrics") or "").splitlines()] or None


def from_genius(song):
    """Plain lyrics with [Verse]/[Chorus] headers, from Genius's page for the song."""
    from html.parser import HTMLParser
    bare = re.sub(r"\s*[(\[](?:feat|ft|with)\.?\s[^)\]]*[)\]]", "", song["title"], flags=re.I).strip()
    hits = next((s.get("hits") or [] for s in (ly_get("https://genius.com/api/search/multi", {"q": f"{bare} {song['artist']}"})
                                                 .get("response") or {}).get("sections") or [] if s.get("type") == "song"), [])
    t, a = norm(bare), norm(song["artist"])
    url = next((h["result"]["url"] for h in hits if (lambda rt, ra: (t in rt or rt in t) and (a in ra or ra in a))(
        norm(h["result"].get("title")), norm(h["result"].get("primary_artist_names") or h["result"].get("primary_artist", {}).get("name")))), None)
    if not url:
        return None

    class Page(HTMLParser):
        def __init__(self):
            super().__init__()
            self.depth, self.skip, self.text = 0, 0, []

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if self.depth:
                self.depth += tag not in ("br", "img")
                self.skip += bool(self.skip) or a.get("data-exclude-from-selection") == "true"
                if tag == "br":
                    self.text.append("\n")
            elif tag == "div" and a.get("data-lyrics-container") == "true":
                self.depth = 1
                self.text.append("\n")

        def handle_endtag(self, tag):
            if self.depth and tag not in ("br", "img"):
                self.depth -= 1
                self.skip = max(0, self.skip - 1)

        def handle_data(self, data):
            if self.depth and not self.skip:
                self.text.append(data)
    page = Page()
    page.feed(ly_get(url, as_json=False))
    text = re.sub(r"\n{3,}", "\n\n", "".join(page.text)).strip()
    return [{"t": -1, "e": None, "text": l.strip()} for l in text.splitlines()] if text else None


# The services TubeView asks (Prism also asks QQ, Bini, Unison and YouTube Music's own).
# Apple Music's lyrics (through Better Lyrics) first: they answer fastest and are the best timed for most songs;
# KuGou and NetEase have the most Asian songs. Prism asks KuGou first.
LYRIC_SOURCES = [("Apple Music", from_apple), ("LyricsPlus", from_lyricsplus), ("KuGou", from_kugou),
                 ("NetEase", from_netease), ("LRCLIB", from_lrclib), ("Genius", from_genius)]
WORD_SOURCES = {"KuGou", "Apple Music", "LyricsPlus", "NetEase"}
HEAD_START = 1.2  # seconds a service has to answer before the next one is asked as well


def lyric_level(lines):
    if not lines:
        return 0
    return 3 if any(l.get("w") for l in lines) else 2 if any(l["t"] >= 0 for l in lines) else 1


@lru_cache(maxsize=128)
def lyrics(artist, track, album, duration):
    song = {"artist": artist.split(",")[0].strip(), "title": clean_title(track), "album": album, "duration": duration}
    ex = ThreadPoolExecutor(len(LYRIC_SOURCES))
    futures, launched = [None] * len(LYRIC_SOURCES), [threading.Event() for _ in LYRIC_SOURCES]
    done, rush = threading.Event(), threading.Event()  # rush: ask the rest now, without their head starts

    def ask(fn):
        try:
            return fn(song)
        except Exception:  # one service down or changed: the others still answer
            return None

    def launch():
        for i, (_, fn) in enumerate(LYRIC_SOURCES):
            if done.is_set():
                return
            futures[i] = ex.submit(ask, fn)
            launched[i].set()
            end = time.time() + HEAD_START
            while time.time() < end and not futures[i].done() and not rush.is_set():
                time.sleep(0.05)
    threading.Thread(target=launch, daemon=True).start()

    def answer(i):
        launched[i].wait()
        try:
            return futures[i].result(timeout=15)
        except Exception:
            return None

    def result(name, lines):
        if lines and lines[0].get("instrumental"):
            return {"source": name, "lines": [], "words": False, "synced": False, "plain": "", "instrumental": True}
        return {"source": name, "lines": lines, "words": lyric_level(lines) == 3, "synced": lyric_level(lines) >= 2,
                "plain": "\n".join(l["text"] for l in lines), "instrumental": False}
    plain, instrumental = None, None
    try:
        for i, (name, _) in enumerate(LYRIC_SOURCES):
            lines = answer(i)
            if lines and lines[0].get("instrumental"):
                instrumental = instrumental or (name, lines)
                continue
            level = lyric_level(lines)
            if level == 3:
                return result(name, lines)
            if level == 2:
                # word timing from a service further down the list still wins: ask those at once
                rush.set()
                for j in range(i + 1, len(LYRIC_SOURCES)):
                    if LYRIC_SOURCES[j][0] in WORD_SOURCES:
                        better = answer(j)
                        if lyric_level(better) == 3:
                            return result(LYRIC_SOURCES[j][0], better)
                return result(name, lines)
            if level == 1 and not plain:
                plain = (name, lines)
        found = plain or instrumental
        if not found:
            raise ValueError("No lyrics found.")
        return result(*found)
    finally:
        done.set()
        ex.shutdown(wait=False)


def channels():
    out = []
    for e in feed("https://www.youtube.com/feed/channels", 500).get("entries") or []:
        cid = e.get("channel_id") or e.get("id") or ""
        if CHANNEL_ID.match(cid):
            thumbs = e.get("thumbnails") or [{}]
            out.append({"id": cid, "name": e.get("title") or e.get("channel") or "", "avatar": thumbs[-1].get("url")})
    return out


def https(u):
    return "https:" + u if u and u.startswith("//") else u


def playlist_card(e):
    thumbs = [t for t in e.get("thumbnails") or [] if t.get("url")]
    owner = e.get("channel") or e.get("uploader") or ""
    return {"id": e["id"], "title": e.get("title") or "", "count": e.get("playlist_count"),
            "channel": "" if owner.startswith("View ") else owner,  # YouTube's "View full playlist" link text
            "thumb": https(max(thumbs, key=lambda t: t.get("width") or 0)["url"]) if thumbs else None}


def playlists(info):
    return [playlist_card(e) for e in info.get("entries") or [] if PLAYLIST_ID.match(e.get("id") or "")]


def channel(cid, tab="videos"):
    info = feed(f"https://www.youtube.com/channel/{cid}/{tab}", 48)
    art = {t.get("id"): https(t.get("url")) for t in info.get("thumbnails") or []}
    out = {"name": info.get("channel") or info.get("title") or "", "handle": info.get("uploader_id"),
           "subscribers": info.get("channel_follower_count"), "verified": bool(info.get("channel_is_verified")),
           "description": (info.get("description") or "")[:600],
           "avatar": art.get("avatar_uncropped"), "banner": art.get("banner_uncropped")}
    if tab == "playlists":
        out["playlists"] = playlists(info)
    else:
        out["videos"] = videos(info)
    return out


def playlist(pid):
    info = feed(f"https://www.youtube.com/playlist?list={pid}", 300)
    return {"title": info.get("title") or "", "channel": info.get("channel") or info.get("uploader") or "",
            "videos": videos(info)}


# --- Search with filters: YouTube's "sp" parameter is a tiny protobuf {1: sort, 2: {1: date, 2: type, 3: length, 8: live}} ---
SORT = {"rating": 1, "date": 2, "views": 3}
DATE = {"hour": 1, "today": 2, "week": 3, "month": 4, "year": 5}
TYPE = {"video": 1, "channel": 2, "playlist": 3}
LENGTH = {"short": 1, "long": 2, "medium": 3}


def search_params(sort="", date="", kind="", length=""):
    inner = b"".join(bytes([tag, table[key]]) for tag, table, key in
                     ((0x08, DATE, date), (0x10, TYPE, kind), (0x18, LENGTH, length)) if key in table)
    if kind == "live":
        inner += b"\x40\x01"
    msg = (bytes([0x08, SORT[sort]]) if sort in SORT else b"") + (bytes([0x12, len(inner)]) + inner if inner else b"")
    return base64.b64encode(msg).decode()


@lru_cache(maxsize=64)
def results(q, sort, date, kind, length):
    sp = search_params(sort, date, kind, length)
    info = feed("https://www.youtube.com/results?" + urlencode({"search_query": q, **({"sp": sp} if sp else {})}), 40)
    vids, chans, lists = [], [], []
    for e in info.get("entries") or []:
        i, url = e.get("id") or "", e.get("url") or ""
        if VIDEO_ID.match(i):
            vids.append(slim(e))
        elif CHANNEL_ID.match(i):
            thumbs = [t for t in e.get("thumbnails") or [] if t.get("url")]
            chans.append({"id": i, "name": e.get("title") or e.get("channel") or "",
                          "avatar": https(thumbs[-1]["url"]) if thumbs else None, "subscribers": e.get("channel_follower_count")})
        elif PLAYLIST_ID.match(i) and "list=" in url:
            lists.append(playlist_card(e))
    return {"videos": vids, "channels": chans, "playlists": lists}


def suggest(q):
    url = "https://suggestqueries.google.com/complete/search?" + urlencode({"client": "firefox", "ds": "yt", "q": q})
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read().decode("utf-8", "replace"))[1][:10]


@lru_cache(maxsize=64)
def comments(vid):
    with ydl(getcomments=True, extractor_args={"youtube": {"max_comments": ["60", "60", "0", "0"], "comment_sort": ["top"]}}) as y:
        e = y.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
    return [{"author": c.get("author") or "", "avatar": c.get("author_thumbnail"), "text": c.get("text") or "",
             "likes": c.get("like_count"), "time": c.get("timestamp"), "pinned": bool(c.get("is_pinned")),
             "uploader": bool(c.get("author_is_uploader")), "hearted": bool(c.get("is_favorited"))}
            for c in e.get("comments") or [] if c.get("parent") == "root"]


def sign_in():
    spec = default_browser()
    if not spec or not (spec["profile"] or WINDOWS):  # on Windows yt-dlp finds the default profile itself
        raise ValueError("Couldn't find your default browser's profile. Supported: "
                         + ", ".join(dict.fromkeys(b[1] for b in BROWSERS)) + ".")
    try:
        feed(":ytsubs", 1, spec)  # raises if that browser has no YouTube login
    except Exception as e:
        hint = ("\n\nOn Windows, Chrome, Edge and Brave lock their cookies away from other apps; Firefox-based browsers work."
                if WINDOWS and spec["browser"] != "firefox" else "")
        raise ValueError(f"Couldn't use your YouTube login from {spec['name']}. "
                         f"Sign in to YouTube there, then try again.{hint}\n\n{e}") from e
    cfg.clear()
    cfg.update(spec)
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg))
    forget("recs")


def sign_out():
    cfg.clear()
    CONFIG.unlink(missing_ok=True)
    forget("recs")


# --- In-page player: ffmpeg remuxes YouTube's separate video+audio streams into one fragmented MP4 ---

_infos = {}  # video id -> (expires, info)
_info_locks = {}  # video id -> lock, so the details sheet and the player don't both look a video up

# The quick way to a video's streams, as Prism (github.com/Yigder/Prism) gets them: one player request as YouTube's
# visionOS app (~0.3 s), whose stream links need no signature or "n" solving, instead of yt-dlp's several requests
# and its JavaScript challenge (seconds, and much longer on a TV). Anything it can't answer as-is (live streams,
# age-restricted or members-only videos, a bot check) goes to yt-dlp as before. Not an official API either.
VISIONOS_VERSION = "1.02"
VISIONOS_UA = f"com.google.visionos.youtube/{VISIONOS_VERSION}(RealityDevice14,1; U; CPU visionOS 25_6_0 like Mac OS X; US)"
vision = {"visitor": None, "off_until": 0}


def vision_post(endpoint, body, visitor):
    client = {"clientName": "VISIONOS", "clientVersion": VISIONOS_VERSION, "clientScreen": "WATCH", "platform": "MOBILE",
              "deviceMake": "Apple", "deviceModel": "RealityDevice14,1", "osName": "visionOS", "osVersion": "25.6.0.23O471",
              "hl": "en", "gl": "US", "utcOffsetMinutes": 0, **({"visitorData": visitor} if visitor else {})}
    headers = {"Content-Type": "application/json", "User-Agent": VISIONOS_UA, "X-Goog-Api-Format-Version": "2",
               "X-YouTube-Client-Name": "101", "X-YouTube-Client-Version": VISIONOS_VERSION,
               **({"X-Goog-Visitor-Id": visitor} if visitor else {})}
    req = urllib.request.Request(f"https://www.youtube.com/youtubei/v1/{endpoint}?prettyPrint=false",
                                 json.dumps({"context": {"client": client}, **body}).encode(), headers)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def vision_player(vid):
    """The player response; without a visitor id YouTube answers with a bot check, so one is kept (and renewed once)."""
    for _ in range(2):
        if not vision["visitor"]:
            vision["visitor"] = vision_post("visitor_id", {}, None)["responseContext"]["visitorData"]
        r = vision_post("player", {"videoId": vid, "contentCheckOk": True, "racyCheckOk": True}, vision["visitor"])
        ps = r.get("playabilityStatus") or {}
        if ps.get("status") != "LOGIN_REQUIRED" or "bot" not in (ps.get("reason") or "").lower():
            return r
        vision["visitor"] = None
    return r


def quick_info(vid):
    """A video's streams through vision_player, shaped like yt-dlp's info (what the players read), or None."""
    # Android only for now: it has no chapters or seek previews (the desktop player shows both), and the browser
    # login only works through yt-dlp
    if PLATFORM != "android" or cfg or time.time() < vision["off_until"]:
        return None
    try:
        r = vision_player(vid)
    except (OSError, ValueError, KeyError):
        return None
    ps, sd, vd = r.get("playabilityStatus") or {}, r.get("streamingData") or {}, r.get("videoDetails") or {}
    if ps.get("status") != "OK":
        if "bot" in (ps.get("reason") or "").lower():
            vision["off_until"] = time.time() + 600  # YouTube is turning this network away: yt-dlp for a while
        return None
    raw = (sd.get("adaptiveFormats") or []) + (sd.get("formats") or [])
    if vd.get("isLive") or vd.get("isUpcoming") or not raw or any(not f.get("url") or re.search(r"[?&]n=", f["url"]) for f in raw):
        return None
    formats = []
    for f in raw:
        m = re.match(r'(\w+)/(\w+); codecs="([^"]+)"', f.get("mimeType", ""))
        if not m:
            continue
        kind, ext, codecs = m.group(1), m.group(2), [c.strip() for c in m.group(3).split(",")]
        track = f.get("audioTrack") or {}
        formats.append({
            "format_id": str(f["itag"]) + ("-drc" if f.get("isDrc") else ""), "url": f["url"], "protocol": "https", "ext": ext,
            "height": f.get("height"), "width": f.get("width"), "fps": f.get("fps"),
            "vcodec": codecs[0] if kind == "video" else "none",
            "acodec": codecs[1] if kind == "video" and len(codecs) > 1 else codecs[0] if kind == "audio" else "none",
            "tbr": (f.get("averageBitrate") or f.get("bitrate") or 0) / 1000,
            "abr": (f.get("averageBitrate") or f.get("bitrate") or 0) / 1000 if kind == "audio" else None,
            "filesize": int(f["contentLength"]) if str(f.get("contentLength", "")).isdigit() else None,
            "language": track.get("id", "").split(".")[0] or None, "format_note": track.get("displayName"),
            "language_preference": (10 if track.get("audioIsDefault") else -1) if track else None,
            "http_headers": {"User-Agent": VISIONOS_UA}})
    subs, auto, spoken = {}, {}, None
    for c in ((r.get("captions") or {}).get("playerCaptionsTracklistRenderer") or {}).get("captionTracks") or []:
        name = "".join(x.get("text", "") for x in (c.get("name") or {}).get("runs") or []) or c.get("name", {}).get("simpleText", "")
        entry = [{"ext": "vtt", "url": c["baseUrl"] + "&fmt=vtt", "name": name.removesuffix(" (auto-generated)")}]
        if c.get("kind") == "asr":
            auto[c["languageCode"]] = entry
            spoken = spoken or c["languageCode"]
        else:
            subs[c["languageCode"]] = entry
    return {"id": vid, "title": vd.get("title") or "", "channel": vd.get("author") or "", "uploader": vd.get("author") or "",
            "channel_id": vd.get("channelId"), "duration": int(vd.get("lengthSeconds") or 0) or None,
            "view_count": int(vd["viewCount"]) if str(vd.get("viewCount", "")).isdigit() else None,
            "description": vd.get("shortDescription") or "", "is_live": False, "formats": formats,
            "subtitles": subs, "automatic_captions": auto, "language": spoken, "quick": True}


def play_info(vid):
    with _info_locks.setdefault(vid, threading.Lock()):  # setdefault is atomic
        hit = _infos.get(vid)
        if hit and hit[0] > time.time():
            return hit[1]
        info = quick_info(vid)
        if not info:
            with ydl() as y:
                info = y.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
        # shortcut: stream URLs last ~6h, so cache 1h; entries are never evicted, fine for a desktop session
        _infos[vid] = (time.time() + 3600, info)
        return info


def caption_tracks(info):
    out = [{"lang": lang, "name": subs[0].get("name") or lang, "auto": False}
           for lang, subs in (info.get("subtitles") or {}).items()
           if lang != "live_chat" and any(s.get("ext") == "vtt" for s in subs)]
    auto = info.get("automatic_captions") or {}
    # auto captions exist in ~150 machine translations; offer the spoken language and English
    for lang in dict.fromkeys([info.get("language"), "en"]):
        if lang and lang in auto and all(o["lang"] != lang for o in out):
            out.append({"lang": lang, "name": f"{auto[lang][0].get('name') or lang} (auto-generated)", "auto": True})
    return out


VTT_TS = re.compile(r"(\d+):(\d\d):(\d\d)\.(\d\d\d)")


def shift_vtt(text, offset):
    """Move cue times back by offset: the player's stream starts at offset seconds, not 0."""
    if offset <= 0:
        return text
    secs = lambda m: int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3]) + int(m[4]) / 1000
    def fmt(m):
        t = max(0.0, secs(m) - offset)
        return f"{int(t // 3600):02d}:{int(t % 3600 // 60):02d}:{t % 60:06.3f}"
    out = []
    for block in text.split("\n\n"):
        timing = next((l for l in block.splitlines() if "-->" in l), None)
        end = VTT_TS.search(timing.split("-->")[1]) if timing else None
        if end and secs(end) <= offset:
            continue  # cue ended before the stream starts
        out.append(VTT_TS.sub(fmt, block))
    return "\n\n".join(out)


@lru_cache(maxsize=32)
def caption_text(vid, lang, auto):
    info = play_info(vid)
    subs = (info.get("automatic_captions" if auto else "subtitles") or {}).get(lang) or []
    vtt = next((s for s in subs if s.get("ext") == "vtt"), None)
    if not vtt:
        raise ValueError("No captions in that language.")
    with urllib.request.urlopen(vtt["url"], timeout=15) as r:
        return r.read().decode("utf-8", "replace")


def storyboard(info):
    """Seek-bar preview sprites: pages of rows x cols tiles, one tile every `interval` seconds."""
    sbs = [f for f in info.get("formats") or [] if f.get("format_id", "").startswith("sb") and f.get("fragments")]
    if not sbs:
        return None
    f = max(sbs, key=lambda f: f.get("width") or 0)
    per_page = f["rows"] * f["columns"]
    return {"w": f["width"], "h": f["height"], "rows": f["rows"], "cols": f["columns"],
            "interval": f["fragments"][0]["duration"] / per_page if f["fragments"][0].get("duration") else 1 / f["fps"],
            "pages": [fr["url"] for fr in f["fragments"]]}


VIDEO_CODECS = ("avc1", "vp9", "vp09")  # H.264 up to 1080p (hardware decoded); VP9 for 1440p/4K, which YouTube has no H.264 for


def heights(info, web=False):
    fs = info.get("formats") or []
    if web and not info.get("is_live"):
        return sorted({f["height"] for f in fs if f.get("height") and (f.get("vcodec") or "").startswith(("vp9", "vp09"))
                       and f.get("protocol") == "https"}, reverse=True)
    if info.get("is_live"):
        return sorted({f["height"] for f in fs if f.get("height") and "m3u8" in f.get("protocol", "")}, reverse=True)
    return sorted({f["height"] for f in fs if f.get("height") and (f.get("vcodec") or "").startswith(VIDEO_CODECS)
                   and f.get("protocol") == "https"}, reverse=True)


def audio_tracks(info):
    """Dubbed videos carry one AAC track per language; YouTube marks the original with language_preference 10."""
    tracks = {}
    for f in info.get("formats") or []:
        if f.get("language") and f.get("vcodec") == "none" and (f.get("acodec") or "").startswith("mp4a"):
            note = (f.get("format_note") or f["language"]).split(",")[0]
            tracks.setdefault(f["language"], {"lang": f["language"], "name": note, "original": (f.get("language_preference") or 0) >= 10})
    return sorted(tracks.values(), key=lambda t: (not t["original"], t["name"])) if len(tracks) > 1 else []


def pick_formats(info, q, lang="", web=False):
    """H.264 + AAC (plays in every browser) at the best height <= q; one muxed format for live/fallback.
    lang picks a dubbed audio track; otherwise the original-language track wins.
    web: VP9 + Opus only, for players without H.264/AAC (the PyPI Qt WebEngine in the packaged apps)."""
    # YouTube's "-drc" (dynamic range compressed) AAC variants fail to decode in browsers
    fs = [f for f in info.get("formats") or [] if "drc" not in f.get("format_id", "")]
    rank = lambda f: (f.get("height") or 0, f.get("tbr") or 0)
    acodec_ok = (lambda c: c == "opus") if web else (lambda c: c.startswith("mp4a"))
    audio = [f for f in fs if f.get("protocol") == "https" and acodec_ok(f.get("acodec") or "") and f.get("vcodec") == "none"]
    audio = [f for f in audio if f.get("language") == lang] or audio
    best_audio = lambda: max(audio, key=lambda f: (f.get("language_preference") or 0, f.get("abr") or 0))
    if q == 0 and not info.get("is_live"):  # audio only
        return [best_audio()] if audio else []
    if info.get("is_live"):
        if web:
            return []  # YouTube streams live only as H.264/AAC
        hls = [f for f in fs if "m3u8" in f.get("protocol", "") and (f.get("height") or 0) <= (q or 144)]
        return [max(hls, key=rank)] if hls else []
    https = [f for f in fs if f.get("protocol") == "https"]
    video = [f for f in https if (f.get("vcodec") or "").startswith(("vp9", "vp09") if web else VIDEO_CODECS)
             and f.get("acodec") == "none" and (f.get("height") or 0) <= q]
    # same height: prefer H.264
    rank = lambda f: (f.get("height") or 0, (f.get("vcodec") or "").startswith("avc1"), f.get("tbr") or 0)
    if video and audio:
        return [max(video, key=rank), best_audio()]
    muxed = [f for f in https if (f.get("vcodec") or "none") != "none" and (f.get("acodec") or "none") != "none"]
    return [max(muxed, key=rank)] if muxed else []


def ffmpeg_cmd(formats, start, audio_only=False, keep_times=False):
    """keep_times: keep the video's own timestamps (for the player's MediaSource buffer, which places
    fragments by them), instead of restarting the clock at 0 for a plain <video src>."""
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error"] + (["-copyts"] if keep_times else [])
    for f in formats:
        hdr = "".join(f"{k}: {v}\r\n" for k, v in (f.get("http_headers") or {}).items())
        cmd += (["-ss", f"{start:.2f}"] if start > 0 else []) + ["-headers", hdr, "-i", f["url"]]
    maps = ["-map", "0:v:0", "-map", "1:a:0"] if len(formats) == 2 else ["-map", "0:a:0"] if audio_only else []
    # live HLS carries ADTS-framed AAC, which MP4 can't hold as-is
    bsf = ["-bsf:a", "aac_adtstoasc"] if "m3u8" in formats[0].get("protocol", "") else []
    return cmd + maps + bsf + ["-c", "copy", "-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof", "pipe:1"]


SB_CATEGORIES = {"sponsor", "selfpromo", "exclusive_access", "interaction", "intro", "outro", "preview", "hook",
                 "filler", "music_offtopic"}


def sponsor_segments(vid, cats):
    """SponsorBlock segments for vid, looked up by hash prefix so the video id itself isn't sent."""
    prefix = hashlib.sha256(vid.encode()).hexdigest()[:4]
    url = f"https://sponsor.ajay.app/api/skipSegments/{prefix}?" + urlencode(
        {"categories": json.dumps(sorted(cats)), "actionTypes": json.dumps(["skip", "mute"])})
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.load(r)
    except (OSError, ValueError):
        return []  # 404 means no segments; SponsorBlock being down must never block playback
    return next((x["segments"] for x in data if x.get("videoID") == vid), [])


def play(vid, q=1080, skip=(), ask=(), start=0):
    """skip/ask: SponsorBlock categories to skip automatically / offer to skip (Tab) in mpv."""
    segs = sponsor_segments(vid, set(skip) | set(ask)) if skip or ask else []
    spec = ";".join(f"{g['segment'][0]:.2f}:{g['segment'][1]:.2f}:{g['category']}:{g['actionType']}:"
                    f"{'skip' if g['category'] in skip else 'ask'}" for g in segs)
    script_opts = f"ytdl_hook-ytdl_path={YTDL}" + (f",tubeview-segments={spec}" if spec else "")
    cmd = ["mpv", f"--script-opts={script_opts}", f"--ytdl-format=bv*[height<={q}]+ba/b" if q else "--ytdl-format=ba/b",
           "--force-window=immediate", f"--script={HERE / 'sponsorblock.lua'}"] + ([] if q else ["--no-video"]) \
        + ([f"--start={start:.1f}"] if start > 0 else [])
    raw = [f"js-runtimes={JS_RUNTIME}"] if JS_RUNTIME else []
    if cfg:
        raw.append(f"cookies-from-browser={cfg['browser']}" + (f":{cfg['profile']}" if cfg.get("profile") else ""))
    if raw:
        cmd.append("--ytdl-raw-options=" + ",".join(raw))
    subprocess.Popen(cmd + [f"https://www.youtube.com/watch?v={vid}"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=not WINDOWS, **NO_WINDOW)


# --- Downloads: saved as MP4 (video) / M4A (audio) so the built-in player can play them offline ---

def videos_dir():
    if WINDOWS:
        return Path.home() / "Videos" / "TubeView"
    try:
        d = subprocess.run(["xdg-user-dir", "VIDEOS"], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        d = ""
    return Path(d if d and d != str(Path.home()) else Path.home() / "Videos") / "TubeView"


DL_DIR = Path(os.environ["TUBEVIEW_DOWNLOADS"]) if os.environ.get("TUBEVIEW_DOWNLOADS") else videos_dir()
DL_INDEX = DL_DIR / ".tubeview-downloads.json"
jobs = {}  # "id:kind" -> progress of downloads in flight (or failed)

# --- The page's settings, history, resume points and playlists: one JSON file, shared by the app window and browser tabs ---
DATA = CONFIG.parent / "data.json"
DATA_KEY = re.compile(r"^[\w:.-]{1,64}$")
data_lock = threading.Lock()


def read_data():
    try:
        return json.loads(DATA.read_text())
    except (OSError, ValueError):
        return {}


def update_data(changes):
    """Merge {key: value} into the data file (None deletes a key). Written via a temp file so a crash can't truncate it."""
    with data_lock:
        data = read_data()
        for k, v in changes.items():
            if v is None:
                data.pop(k, None)
            else:
                data[k] = v
        DATA.parent.mkdir(parents=True, exist_ok=True)
        tmp = DATA.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(DATA)
dl_lock = threading.Lock()


def dl_items():
    try:
        items = json.loads(DL_INDEX.read_text())
    except (OSError, ValueError):
        return []
    return [i for i in items if (DL_DIR / i["file"]).is_file()]


def dl_path(vid, kind):
    item = next((i for i in dl_items() if i["id"] == vid and i["kind"] == kind), None)
    path = (DL_DIR / item["file"]).resolve() if item else None
    # index entries are only trusted to point inside the downloads folder
    return path if path and path.parent == DL_DIR.resolve() and path.is_file() else None


def download(vid, kind, q, web=False):
    key = f"{vid}:{kind}"
    job = jobs[key] = {"key": key, "id": vid, "kind": kind, "status": "starting", "progress": 0, "title": ""}

    def hook(d):
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        job.update(title=d.get("info_dict", {}).get("title") or job["title"], status="downloading",
                   progress=(d.get("downloaded_bytes") or 0) / total if total else 0, speed=d.get("_speed_str"))
    nodrc = "[format_id!*=drc]"  # see pick_formats
    if not shutil.which("ffmpeg"):  # can't merge separate video and audio: take a single-file format
        fmt = "ba[ext=m4a]/ba" if kind == "audio" else f"b[height<={q}][ext=mp4]/b"
    elif web:  # VP9 + Opus, playable in builds without H.264/AAC
        fmt = "ba[acodec=opus]/ba" if kind == "audio" else f"bv*[height<={q}][vcodec^=vp9]+ba[acodec=opus]/bv*[height<={q}]+ba/b"
    else:
        fmt = (f"ba[ext=m4a]{nodrc}/ba" if kind == "audio" else
               f"bv*[height<={q}][vcodec^=avc1]+ba[ext=m4a]{nodrc}/b[height<={q}][ext=mp4]/bv*[height<={q}]+ba/b")
    try:
        DL_DIR.mkdir(parents=True, exist_ok=True)
        with ydl(format=fmt, skip_download=False, merge_output_format="mp4", progress_hooks=[hook], noprogress=True,
                 outtmpl=str(DL_DIR / "%(title).120B [%(id)s].%(ext)s")) as y:
            info = y.extract_info(f"https://www.youtube.com/watch?v={vid}")
        path = Path(info["requested_downloads"][0]["filepath"])
        with dl_lock:
            items = [i for i in dl_items() if not (i["id"] == vid and i["kind"] == kind)]
            items.insert(0, {"id": vid, "kind": kind, "file": path.name, "title": info.get("title") or "",
                             "channel": info.get("channel") or "", "duration": info.get("duration"), "at": time.time()})
            DL_INDEX.write_text(json.dumps(items))
        jobs.pop(key, None)
    except Exception as e:
        job.update(status="error", error=str(e)[-300:])


def delete_download(vid, kind):
    path = dl_path(vid, kind)
    with dl_lock:
        if path:
            path.unlink(missing_ok=True)
        DL_INDEX.write_text(json.dumps([i for i in dl_items() if not (i["id"] == vid and i["kind"] == kind)]))


# --- New-upload alerts: check subscriptions every 15 minutes while TubeView runs ---
SEEN = CONFIG.parent / "seen-uploads.json"
open_requests = []  # videos picked from a notification, for the page to open (it polls /api/open-request)


def notify_upload(v):
    """Show one desktop notification; if its Watch button is clicked, ask the page to play the video."""
    icon = CONFIG.parent / "thumbs" / f"{v['id']}.jpg"
    try:
        icon.parent.mkdir(parents=True, exist_ok=True)
        if not icon.exists():
            icon.write_bytes(urllib.request.urlopen(f"https://i.ytimg.com/vi/{v['id']}/mqdefault.jpg", timeout=10).read())
    except OSError:
        icon = None
    try:
        r = subprocess.run(["notify-send", "-a", "TubeView", "-i", str(icon or "multimedia-video-player"), "-A", "watch=Watch",
                            f"New from {v['channel']}", v["title"]], capture_output=True, text=True, timeout=3600)
    except (OSError, subprocess.TimeoutExpired):
        return
    if r.stdout.strip() == "watch":
        open_requests.append(v)


def upload_alerts():
    while True:
        try:
            if cfg and read_data().get("pref:notify", True) and shutil.which("notify-send"):  # Linux desktops
                vids = [v for v in videos(feed(":ytsubs", 30)) if not v["live"]]
                try:
                    seen = json.loads(SEEN.read_text())
                except (OSError, ValueError):
                    seen = None  # first check: remember what's there without announcing it
                if seen is not None:
                    new = [v for v in vids if v["id"] not in set(seen)]
                    for v in new[:5]:
                        threading.Thread(target=notify_upload, args=(v,), daemon=True).start()
                    if len(new) > 5:
                        subprocess.run(["notify-send", "-a", "TubeView", "-i", "multimedia-video-player",
                                        f"{len(new) - 5} more new uploads", "Open Subscriptions in TubeView to see them."])
                SEEN.write_text(json.dumps(list(dict.fromkeys([v["id"] for v in vids] + (seen or [])))[:500]))
        except Exception:
            pass  # offline or signed out: try again next round
        time.sleep(900)


def auto_update_ytdlp():
    """Once a day, update yt-dlp in the background (YouTube changes often break older versions)."""
    stamp = CONFIG.parent / "ytdlp-checked"
    if sys.prefix == sys.base_prefix:
        return  # not running from TubeView's own venv: don't touch the system's packages
    try:
        if time.time() - stamp.stat().st_mtime < 86400:
            return
    except OSError:
        pass
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "yt-dlp[default]"],
                       capture_output=True, timeout=600)
    if r.returncode == 0:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()


class AuthError(Exception):
    """A sign-in, or a signed-in request, that didn't work."""


# --- YouTube TV sign-in: the way SmartTube and NewTube get your real YouTube home page. They sign in as YouTube's own
# TV app (its sign-in client comes from youtube.com/tv, as the TV app itself loads it) and ask YouTube for that app's
# home page. Not an official API: YouTube can change or block it at any time, and TubeView then falls back. ---
YTV = CONF_DIR / "youtube-tv.json"
YTV_UA = ("Mozilla/5.0 (ChromiumStylePlatform) Cobalt/25.lts.30.1034943-gold (unlike Gecko), "
          "Unknown_TV_Unknown_0/Unknown (Unknown, Unknown)")
YTV_VERSION = "7.20260901.15.00"
YTV_SCOPE = "http://gdata.youtube.com https://www.googleapis.com/auth/youtube-paid-content"
ytv_login = {}  # the code shown while a YouTube TV sign-in is in progress


def ytv_session():
    try:
        return json.loads(YTV.read_text())
    except (OSError, ValueError):
        return {}


def save_ytv(s):
    YTV.parent.mkdir(parents=True, exist_ok=True)
    YTV.write_text(json.dumps(s))


def ytv_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": YTV_UA, "Referer": "https://www.youtube.com/tv"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def ytv_client():
    """The TV app's sign-in client id and secret, read from the script youtube.com/tv loads."""
    page = ytv_get("https://www.youtube.com/tv")
    m = (re.search(r'id="base-js" src="(.*?)"', page) or re.search(r"\.src = '(.*?m=base)'", page)
         or re.search(r"\.src = '(.*?)'; .\.id = 'base-js'", page))
    if not m:
        raise AuthError("Couldn't find YouTube TV's sign-in (the TV app's page changed).")
    js = ytv_get(urljoin("https://www.youtube.com/tv", m.group(1).replace("\\/", "/")))
    c = re.search(r'clientId:"([-\w]+\.apps\.googleusercontent\.com)",\n?[$\w]+:"(\w+)"', js)
    if not c:
        raise AuthError("Couldn't find YouTube TV's sign-in (the TV app's script changed).")
    return {"client_id": c.group(1), "client_secret": c.group(2)}


def post_json(url, body, headers=None):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except ValueError:
            raise AuthError(f"YouTube returned {e.code}") from e


def ytv_start():
    c = ytv_client()
    r = post_json("https://www.youtube.com/o/oauth2/device/code",
                  {"client_id": c["client_id"], "device_id": secrets.token_hex(16), "model_name": "ytlr::", "scope": YTV_SCOPE})
    if "device_code" not in r:
        raise AuthError(r.get("error_description") or r.get("error") or "Couldn't start sign-in.")
    ytv_login.clear()
    ytv_login.update(code=r["user_code"], url=r.get("verification_url") or "https://yt.be/activate",
                     expires=time.time() + r.get("expires_in", 1800), status="waiting")

    def poll():
        wait = r.get("interval", 5)
        while time.time() < ytv_login.get("expires", 0) and ytv_login.get("code") == r["user_code"]:
            time.sleep(wait)
            try:
                t = post_json("https://www.youtube.com/o/oauth2/token", {"code": r["device_code"], **c,
                                                                         "grant_type": "http://oauth.net/grant_type/device/1.0"})
            except (OSError, AuthError):
                continue  # a network blip: ask again
            if "access_token" in t:
                save_ytv({**c, "refresh_token": t.get("refresh_token"), "access_token": t["access_token"],
                          "expires_at": time.time() + t.get("expires_in", 3600) - 60})
                ytv_forget()
                try:
                    ytv_account()
                except (OSError, AuthError, ValueError):
                    pass  # asked again the next time the page asks who's signed in
                ytv_login["status"] = "done"
                return
            if t.get("error") == "slow_down":
                wait += 5
            elif t.get("error") not in ("authorization_pending", None):
                ytv_login["status"] = t.get("error_description") or t.get("error")
                return
        ytv_login["status"] = "expired"
    threading.Thread(target=poll, daemon=True).start()
    return {k: ytv_login[k] for k in ("code", "url")}


def ytv_account():
    """The signed-in account's name, picture and channel (for the account button and "Your channel"), kept in the
    session so it's asked for once: the TV app's account list, then the channel its handle points to."""
    s = ytv_session()
    if not s or "name" in s:
        return {k: s.get(k) for k in ("name", "avatar", "channel_id")} if s else None
    d = ytv_post("account/accounts_list", {"accountReadMask": {"returnOwner": True, "returnBrandAccounts": True, "returnPersonaAccounts": False}})
    items = [c.get("accountItem") or {} for sec in (d.get("contents") or [{}])[0].get("accountSectionListRenderer", {}).get("contents") or []
             for c in (sec.get("accountItemSectionRenderer") or {}).get("contents") or []]
    me = next((a for a in items if a.get("isSelected")), items[0] if items else {})
    handle = yt_text(me.get("channelHandle"))
    channel = None
    if handle.startswith("@"):
        try:  # the handle's channel page, as YouTube resolves it
            r = post_json("https://www.youtube.com/youtubei/v1/navigation/resolve_url?prettyPrint=false",
                          {"context": {"client": {"clientName": "WEB", "clientVersion": "2.20260901.00.00", "hl": "en", "gl": "US"}},
                           "url": "https://www.youtube.com/" + handle})
            channel = ((r.get("endpoint") or {}).get("browseEndpoint") or {}).get("browseId")
        except (OSError, AuthError):
            pass
    thumbs = (me.get("accountPhoto") or {}).get("thumbnails") or [{}]
    s.update(name=yt_text(me.get("accountName")) or "YouTube TV", avatar=https(thumbs[-1].get("url")),
             channel_id=channel if channel and CHANNEL_ID.match(channel) else None)
    save_ytv(s)
    return {k: s.get(k) for k in ("name", "avatar", "channel_id")}


def me_ytv():
    if not ytv_session():
        return None
    try:
        return ytv_account()
    except (OSError, AuthError, ValueError):
        return {"name": "YouTube TV", "avatar": None, "channel_id": None}


def ytv_forget():
    forget("tvhome", "tvsubs")
    for k in [k for k in signed_cache if k.startswith("tv:")]:
        signed_cache.pop(k, None)


def ytv_sign_out():
    YTV.unlink(missing_ok=True)
    ytv_forget()


def ytv_token():
    s = ytv_session()
    if not s:
        raise AuthError("not signed in")
    if time.time() < s.get("expires_at", 0):
        return s["access_token"]
    t = post_json("https://www.youtube.com/o/oauth2/token", {"refresh_token": s.get("refresh_token"), "client_id": s["client_id"],
                                                              "client_secret": s["client_secret"], "grant_type": "refresh_token"})
    if "access_token" not in t:
        if t.get("error") in ("invalid_grant", "unauthorized_client"):
            ytv_sign_out()  # signed out elsewhere, or YouTube stopped accepting this
        raise AuthError(t.get("error_description") or t.get("error") or "expired")
    s.update(access_token=t["access_token"], expires_at=time.time() + t.get("expires_in", 3600) - 60)
    save_ytv(s)
    return s["access_token"]


def ytv_post(path, body):
    return post_json(f"https://www.youtube.com/youtubei/v1/{path}?prettyPrint=false",
                     {"context": {"client": {"clientName": "TVHTML5", "clientVersion": YTV_VERSION, "userAgent": YTV_UA,
                                             "hl": "en", "gl": "US"}}, **body},
                     {"Authorization": "Bearer " + ytv_token(), "User-Agent": YTV_UA, "Referer": "https://www.youtube.com/tv",
                      "X-YouTube-Client-Name": "7", "X-YouTube-Client-Version": YTV_VERSION})


def ytv_browse(body):
    return ytv_post("browse", body)


def yt_text(t):
    if isinstance(t, str):
        return t
    t = t or {}
    return t.get("simpleText") or t.get("content") or "".join(r.get("text", "") for r in t.get("runs") or [])


def clock_seconds(t):
    parts = t.split(":")
    return sum(int(p) * 60 ** i for i, p in enumerate(reversed(parts))) if all(p.isdigit() for p in parts) else None


def count(t):
    m = re.match(r"([\d.,]+)\s*([KMB]?)", t.replace(" ", " "))
    if not m:
        return None
    n = float(m.group(1).replace(",", "")) if m.group(1).replace(",", "").replace(".", "", 1).isdigit() else None
    return int(n * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2)]) if n is not None else None


def tv_tile(it):
    """A video on the TV app's home page -> the same shape as everywhere else in TubeView, or None."""
    t = it.get("tileRenderer")
    if t:
        vid = ((t.get("onSelectCommand") or {}).get("watchEndpoint") or {}).get("videoId") or t.get("contentId") or ""
        kind = t.get("contentType", "TILE_CONTENT_TYPE_VIDEO")
        if kind not in ("TILE_CONTENT_TYPE_VIDEO", "TILE_CONTENT_TYPE_SHORTS") or not VIDEO_ID.match(vid):
            return None
        meta = (t.get("metadata") or {}).get("tileMetadataRenderer") or {}
        lines = [" ".join(yt_text((i.get("lineItemRenderer") or {}).get("text")) for i in (l.get("lineRenderer") or {}).get("items") or [])
                 for l in meta.get("lines") or []]
        overlays = ((t.get("header") or {}).get("tileHeaderRenderer") or {}).get("thumbnailOverlays") or []
        badge = next(((o["thumbnailOverlayTimeStatusRenderer"]) for o in overlays if "thumbnailOverlayTimeStatusRenderer" in o), {})
        dur, style = yt_text(badge.get("text")), badge.get("style", "")
        views = next((count(x) for l in lines[1:] for x in l.split("•") if "view" in x.lower()), None)
        return {"id": vid, "title": yt_text(meta.get("title")), "channel": lines[0].strip() if lines else "",
                "duration": clock_seconds(dur) if dur else None, "views": views, "channel_id": None,
                "live": "is_live" if style == "LIVE" else "is_upcoming" if style == "UPCOMING" else None,
                "short": kind == "TILE_CONTENT_TYPE_SHORTS" or style == "SHORTS"}
    g = it.get("gridVideoRenderer")  # older TV pages
    if g and VIDEO_ID.match(g.get("videoId") or ""):
        overlays = g.get("thumbnailOverlays") or []
        dur = yt_text(g.get("lengthText")) or next((yt_text(o["thumbnailOverlayTimeStatusRenderer"].get("text"))
                                                  for o in overlays if "thumbnailOverlayTimeStatusRenderer" in o), "")
        return {"id": g["videoId"], "title": yt_text(g.get("title")), "channel": yt_text(g.get("shortBylineText")),
                "duration": clock_seconds(dur) if dur else None, "views": count(yt_text(g.get("viewCountText"))),
                "channel_id": (((g.get("shortBylineText") or {}).get("runs") or [{}])[0].get("navigationEndpoint") or {})
                .get("browseEndpoint", {}).get("browseId"), "live": None}
    lk = it.get("lockupViewModel")
    if lk and lk.get("contentType") == "LOCKUP_CONTENT_TYPE_VIDEO" and VIDEO_ID.match(lk.get("contentId") or ""):
        meta = ((lk.get("metadata") or {}).get("lockupMetadataViewModel") or {})
        return {"id": lk["contentId"], "title": yt_text(meta.get("title")), "channel": "", "duration": None,
                "views": None, "channel_id": None, "live": None}
    return None


def tv_shelves(node, out):
    """Every row of videos ("shelf") in a TV app page, in order."""
    if isinstance(node, list):
        for x in node:
            tv_shelves(x, out)
    elif isinstance(node, dict):
        sh = node.get("shelfRenderer")
        if sh:
            c = sh.get("content") or {}
            items = next((c[k].get("items") for k in ("horizontalListRenderer", "gridRenderer", "expandedShelfContentsRenderer")
                          if k in c), None) or []
            head = ((sh.get("headerRenderer") or {}).get("shelfHeaderRenderer") or {})
            vids = [v for v in map(tv_tile, items) if v]
            if vids:
                title = (yt_text(sh.get("title")) or yt_text(head.get("title"))
                         or yt_text(((head.get("avatarLockup") or {}).get("avatarLockupRenderer") or {}).get("title")))
                out.append({"title": title, "videos": vids})
            return
        for v in node.values():
            tv_shelves(v, out)
    return out


def tv_videos(node, out=None, seen=None):
    """Every video in a TV app page, in order."""
    out, seen = ([], set()) if out is None else (out, seen)
    if isinstance(node, list):
        for x in node:
            tv_videos(x, out, seen)
    elif isinstance(node, dict):
        v = tv_tile(node) if ("tileRenderer" in node or "gridVideoRenderer" in node) else None
        if v:
            if v["id"] not in seen:
                seen.add(v["id"])
                out.append(v)
            return out
        for x in node.values():
            tv_videos(x, out, seen)
    return out


def tv_list(body, pages=4):
    """A TV app list (subscriptions, history, a playlist) with the next few pages it points to."""
    d = ytv_browse(body)
    vids = tv_videos(d.get("contents"))
    for _ in range(pages):
        token = re.search(r'"nextContinuationData":\{"continuation":"([^"]+)"', json.dumps(d.get("continuationContents") or d.get("contents"),
                                                                                         separators=(",", ":")))
        if not token:
            break
        try:
            d = ytv_browse({"continuation": token.group(1)})
        except (OSError, AuthError):
            break
        more = tv_videos(d.get("continuationContents"), [], {v["id"] for v in vids})
        if not more:
            break
        vids += more
    return vids


def tv_tabs(d):
    sections = (((d.get("contents") or {}).get("tvBrowseRenderer") or {}).get("content") or {}).get("tvSecondaryNavRenderer", {}).get("sections") or []
    return [t.get("tabRenderer") or t.get("expandableTabRenderer") or {}
            for sec in sections for t in (sec.get("tvSecondaryNavSectionRenderer") or {}).get("tabs") or []]


def tv_subs():
    """New uploads from your subscriptions: the TV app's Subscriptions page."""
    def build():
        ids = {c["name"]: c["id"] for c in tv_channels()}  # tiles name the channel but don't link it
        return [{**v, "channel_id": v["channel_id"] or ids.get(v["channel"])} for v in tv_list({"browseId": "FEsubscriptions"}, 2)[:150]]
    return kept("tvsubs", 600, build)


def tv_channels():
    """Your subscriptions: on the TV app's Subscriptions page, one tab per channel after "All"."""
    def build():
        out = []
        for t in tv_tabs(ytv_browse({"browseId": "FEsubscriptions"}))[1:]:
            params = unquote(((t.get("endpoint") or {}).get("browseEndpoint") or {}).get("params") or "")
            try:  # the tab's params hold the channel id
                m = re.search(rb"UC[\w-]{22}", base64.urlsafe_b64decode(params + "=" * (-len(params) % 4)))
            except ValueError:
                m = None
            if m:
                thumbs = (t.get("thumbnail") or {}).get("thumbnails") or [{}]
                out.append({"id": m.group().decode(), "name": yt_text(t.get("title")), "avatar": https(thumbs[-1].get("url"))})
        return out
    return cached("tv:channels", 600, build)


TV_LIBRARY = {"history": {"browseId": "FEmy_youtube", "params": "cAg="}, "watchlater": {"browseId": "VLWL"}, "liked": {"browseId": "VLLL"}}


def tv_library(name):
    return cached("tv:" + name, 120, lambda: tv_list(TV_LIBRARY[name]))


def tv_liked_music():
    """Your liked songs (YouTube Music's "Liked music" list), or your liked videos if the TV app won't give that list."""
    def build():
        try:
            vids = tv_list({"browseId": "VLLM"}, 8)
        except (OSError, AuthError):
            vids = []
        return [{"id": v["id"], "title": v["title"], "artist": artist_name(v.get("channel")), "album": "",
                 "duration": v.get("duration"), "channel_id": v.get("channel_id")} for v in vids or tv_library("liked")]
    return cached("tv:likedmusic", 300, build)


def tv_playlists():
    """Your playlists: the TV app's Playlists page."""
    def build():
        out, seen = [], set()
        def walk(n):
            if isinstance(n, list):
                for x in n:
                    walk(x)
            elif isinstance(n, dict):
                t = n.get("tileRenderer")
                if t and t.get("contentType") == "TILE_CONTENT_TYPE_PLAYLIST":
                    cmd = t.get("onSelectCommand") or {}
                    pid = (cmd.get("browseEndpoint") or {}).get("browseId", "").removeprefix("VL") or (cmd.get("watchEndpoint") or {}).get("playlistId", "")
                    if PLAYLIST_ID.match(pid) and pid not in seen:
                        seen.add(pid)
                        meta = (t.get("metadata") or {}).get("tileMetadataRenderer") or {}
                        head = (t.get("header") or {}).get("tileHeaderRenderer") or {}
                        thumbs = (head.get("thumbnail") or {}).get("thumbnails") or [{}]
                        texts = [yt_text((i.get("lineItemRenderer") or {}).get("text")) for l in meta.get("lines") or []
                                 for i in (l.get("lineRenderer") or {}).get("items") or []]
                        n_vids = next((count(x) for x in texts + [yt_text((o.get("thumbnailOverlayTimeStatusRenderer") or {}).get("text"))
                                                                  for o in head.get("thumbnailOverlays") or []] if re.search(r"\d+ video", x or "")), None)
                        out.append({"id": pid, "title": yt_text(meta.get("title")), "count": n_vids, "channel": "",
                                    "thumb": https(thumbs[-1].get("url"))})
                    return
                for x in n.values():
                    walk(x)
        walk(ytv_browse({"browseId": "FEplaylist_aggregation"}).get("contents"))
        return out
    return cached("tv:playlists", 300, build)


def tv_playlist(pid):
    def build():
        title = next((p["title"] for p in tv_playlists() if p["id"] == pid), "")
        return {"title": title, "channel": "", "videos": tv_list({"browseId": "VL" + pid})}
    return cached("tv:playlist:" + pid, 120, build)


def tv_action(path, body, *forget_keys):
    r = ytv_post(path, body)
    if r.get("error"):
        raise AuthError((r["error"].get("message") if isinstance(r["error"], dict) else str(r["error"])) or "That didn't work.")
    for k in forget_keys:
        signed_cache.pop(k, None)
    return r


def tv_rate(vid, like):
    tv_action("like/like" if like else "like/removelike", {"target": {"videoId": vid}}, "tv:liked")


def tv_subscribe(cid, on):
    tv_action("subscription/subscribe" if on else "subscription/unsubscribe", {"channelIds": [cid], "params": ""}, "tv:channels")
    forget("tvsubs")


def tv_playlist_add(pid, vid):
    tv_action("browse/edit_playlist", {"playlistId": pid, "actions": [{"addedVideoId": vid, "action": "ACTION_ADD_VIDEO"}]},
              "tv:playlist:" + pid, "tv:playlists")


def tv_home():
    """Your YouTube home page, as the TV app gets it: rows of recommendations, plus the next page of rows."""
    def build():
        d = ytv_browse({"browseId": "default"})
        rows = tv_shelves(d.get("contents"), [])
        # the page's first rows come with a token for the next ones (rows have their own, for more of that row)
        token, stack = None, [d.get("contents")]
        while stack and not token:
            n = stack.pop()
            if isinstance(n, dict):
                if "sectionListRenderer" in n:
                    conts = n["sectionListRenderer"].get("continuations") or [{}]
                    token = (conts[0].get("nextContinuationData") or {}).get("continuation") or ""
                stack.extend(n.values())
            elif isinstance(n, list):
                stack.extend(n)
        if token:
            try:
                rows += tv_shelves(ytv_browse({"continuation": token}), [])
            except (OSError, AuthError):
                pass
        return {"rows": rows} if rows else None
    return kept("tvhome", 1800, build) or {"rows": []}


# --- signed-in lists, kept for a few minutes ---
signed_cache = {}

def cached(key, seconds, fn):
    hit = signed_cache.get(key)
    if hit and time.time() - hit[0] < seconds:
        return hit[1]
    value = fn()
    signed_cache[key] = (time.time(), value)
    return value


# --- Phone remote: a second server on the local network, only while enabled, for phones paired with a code ---
REMOTE_PORT = 8766
REMOTE = CONFIG.parent / "remote.json"  # {"code": pairing code, "tokens": [tokens of paired phones]}
REMOTE_ACTIONS = {"toggle", "next", "prev", "seek", "volume", "shuffle", "repeat", "play", "queue", "jump", "stop",
                  "video_play", "video_toggle", "video_seek", "video_skip", "video_next", "video_prev", "video_close"}
# what the remote shows as this device's name: the Android app passes the phone's or TV's name (hostname is "localhost" there)
DEVICE_NAME = os.environ.get("TUBEVIEW_DEVICE_NAME") or socket.gethostname()
remote = {"server": None, "udp": None, "state": {}, "cmds": [], "fails": []}
remote_lock = threading.Lock()


def remote_conf():
    try:
        return json.loads(REMOTE.read_text())
    except (OSError, ValueError):
        return {"tokens": []}


def save_remote(conf):
    REMOTE.parent.mkdir(parents=True, exist_ok=True)
    # 0600: the tokens let a phone control TubeView
    with os.fdopen(os.open(REMOTE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(conf, f)


def lan_ip():
    try:  # the address this computer uses on the local network (a UDP "connect" sends nothing)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def remote_info():
    conf = remote_conf()
    return {"on": bool(remote["server"]), "ip": lan_ip(), "port": REMOTE_PORT, "code": conf.get("code"),
            "paired": len(conf.get("tokens", [])), "name": DEVICE_NAME}


def remote_enable(on):
    with remote_lock:
        if on and not remote["server"]:
            conf = remote_conf()
            conf["code"] = f"{secrets.randbelow(10 ** 6):06d}"
            save_remote(conf)
            srv = ThreadingHTTPServer(("0.0.0.0", REMOTE_PORT), RemoteHandler)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            remote["server"] = srv
            threading.Thread(target=remote_discovery, daemon=True).start()
        elif not on and remote["server"]:
            remote["server"].shutdown()
            remote["server"].server_close()
            remote["server"] = None
            if remote["udp"]:
                remote["udp"].close()
                remote["udp"] = None


def remote_discovery():
    """Answer "TUBEVIEW?" broadcasts so the Android app can find this computer on the network."""
    try:
        udp = remote["udp"] = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        udp.bind(("0.0.0.0", REMOTE_PORT))
        while remote["server"]:
            msg, addr = udp.recvfrom(64)
            if msg == b"TUBEVIEW?":
                udp.sendto(b"TUBEVIEW " + json.dumps({"name": DEVICE_NAME, "port": REMOTE_PORT}).encode(), addr)
    except OSError:
        pass  # closed when the remote is turned off


def valid_song(s):
    return isinstance(s, dict) and VIDEO_ID.match(str(s.get("id", ""))) and all(
        isinstance(s.get(k, ""), (str, int, float, type(None))) for k in ("title", "artist", "album", "duration", "channel_id"))


def remote_video(v):
    """Just the fields a remote's "play this video" needs, so a phone can't hand the page anything else."""
    if not (isinstance(v, dict) and VIDEO_ID.match(str(v.get("id", "")))):
        return None
    pick = {k: v.get(k) for k in ("id", "title", "channel", "channel_id", "duration", "views")}
    return {k: x for k, x in pick.items() if isinstance(x, (str, int, float)) and not isinstance(x, bool)}


class RemoteHandler(BaseHTTPRequestHandler):
    """What a paired phone can do: see what's playing, send playback commands, search songs. Nothing else."""

    def send(self, code, body, ctype="application/json"):
        Handler.send(self, code, body, ctype)

    def authed(self):
        auth = self.headers.get("Authorization") or ""
        token = auth.removeprefix("Bearer ").strip()
        return bool(token) and any(secrets.compare_digest(token, t) for t in remote_conf().get("tokens", []))

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n)) if 0 < n <= 200_000 else {}
        except ValueError:
            return {}

    def do_GET(self):
        url = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path in ("/", "/remote"):
            return self.send(200, (WEB_DIR / "remote.html").read_bytes(), "text/html; charset=utf-8")
        if url.path == "/api/ping":
            return self.send(200, {"app": "TubeView", "name": DEVICE_NAME})
        if not self.authed():
            return self.send(401, {"error": "not paired"})
        if url.path == "/api/state":
            self.send(200, {**remote["state"], "name": DEVICE_NAME})
        elif url.path in ("/api/msearch", "/api/search") and qs.get("q", "").strip():
            try:
                q = qs["q"].strip()[:200]
                self.send(200, music_search(q) if url.path == "/api/msearch" else search(q))
            except Exception as e:
                self.send(502, {"error": str(e)})
        else:
            self.send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/pair":
            now = time.time()
            remote["fails"] = [t for t in remote["fails"] if now - t < 600]
            if len(remote["fails"]) >= 5:  # guessing a 6-digit code: 5 tries per 10 minutes
                return self.send(429, {"error": "Too many wrong codes. Try again in a few minutes."})
            conf = remote_conf()
            if not conf.get("code") or not secrets.compare_digest(str(self.body().get("code", "")), conf["code"]):
                remote["fails"].append(now)
                return self.send(403, {"error": "Wrong code."})
            token = secrets.token_urlsafe(32)
            conf["tokens"] = (conf.get("tokens", []) + [token])[-20:]
            save_remote(conf)
            return self.send(200, {"token": token, "name": DEVICE_NAME})
        if not self.authed():
            return self.send(401, {"error": "not paired"})
        if path == "/api/cmd":
            c = self.body()
            ok = c.get("action") in REMOTE_ACTIONS and all(valid_song(s) for s in (c.get("songs") or []) + ([c["song"]] if "song" in c else []))
            if "videos" in c:
                vids = [remote_video(v) for v in c["videos"]] if isinstance(c["videos"], list) else [None]
                ok = ok and bool(vids) and None not in vids
                c["videos"] = vids[:50]
            if not ok:
                return self.send(400, {"error": "bad command"})
            remote["cmds"] = (remote["cmds"] + [c])[-50:]
            self.send(200, {"ok": True})
        else:
            self.send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


class Handler(BaseHTTPRequestHandler):
    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def trusted(self):
        # Host check blocks DNS rebinding (other sites reading your subscriptions);
        # Origin check blocks other sites POSTing (launching mpv, signing out)
        return self.headers.get("Host") in HOSTS and self.headers.get("Origin") in (None, *(f"http://{h}" for h in HOSTS))

    def do_GET(self):
        url = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(url.query).items()}
        vid = qs.get("id", "")
        if not self.trusted():
            return self.send(403, {"error": "forbidden"})
        try:
            if url.path == "/":
                self.send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/search" and qs.get("q", "").strip():
                self.send(200, search(qs["q"].strip()[:200]))
            elif url.path == "/api/video" and VIDEO_ID.match(vid):
                self.send(200, video(vid))
            elif url.path == "/api/data":
                self.send(200, read_data())
            elif url.path == "/api/me":
                self.send(200, {"signed_in": bool(cfg), "browser": (cfg or default_browser() or {}).get("name"),
                                "ytv": me_ytv(), "ytv_login": ytv_login.get("status"),
                                "ytdlp": yt_dlp.version.__version__, "config": str(CONFIG.parent), "remote": bool(remote["server"]),
                                "mpv": bool(shutil.which("mpv")), "packaged": bool(FROZEN), "platform": PLATFORM,
                                "ffmpeg": bool(shutil.which("ffmpeg"))})
            elif url.path == "/api/formats" and VIDEO_ID.match(vid):
                # direct stream URLs for a native player (the Android app), which plays video+audio itself
                info = play_info(vid)
                fs = pick_formats(info, int(qs.get("q") or 1080), qs.get("lang", ""))
                if not fs:
                    return self.send(502, {"error": "No playable format for this video."})
                self.send(200, {"live": bool(info.get("is_live")), "hls": fs[0].get("manifest_url") if info.get("is_live") else None,
                                "video": fs[0]["url"], "audio": fs[1]["url"] if len(fs) > 1 else None,
                                "headers": fs[0].get("http_headers") or {}, "duration": info.get("duration"),
                                "heights": heights(info), "captions": caption_tracks(info),
                                "chapters": [{"start": c["start_time"], "title": c.get("title") or ""} for c in info.get("chapters") or []]})
            elif url.path == "/api/sponsor" and VIDEO_ID.match(vid):
                cats = [c for c in qs.get("cats", "").split(",") if c in SB_CATEGORIES]
                self.send(200, [{"s": g["segment"][0], "e": g["segment"][1], "cat": g["category"], "type": g["actionType"]}
                                for g in sponsor_segments(vid, set(cats))] if cats else [])
            elif url.path == "/api/remote":
                self.send(200, remote_info())
            elif url.path == "/api/recs":  # the signed-in YouTube home page
                self.send(200, kept("recs", 1800, lambda: videos(feed(":ytrec", 96))))
            elif url.path == "/api/foryou":
                self.send(200, for_you())
            elif url.path == "/api/tvhome":
                self.send(200, tv_home())
            elif url.path == "/api/subs":
                self.send(200, videos(feed(":ytsubs", 120)) if cfg else tv_subs())
            elif url.path == "/api/channels":
                self.send(200, channels() if cfg else tv_channels())
            elif url.path == "/api/playinfo" and VIDEO_ID.match(vid):
                info = play_info(vid)
                self.send(200, {"duration": info.get("duration"), "live": bool(info.get("is_live")),
                                "heights": heights(info, qs.get("web") == "1"), "captions": caption_tracks(info), "audio": audio_tracks(info),
                                "chapters": [{"start": c["start_time"], "end": c["end_time"], "title": c.get("title") or ""}
                                             for c in info.get("chapters") or []],
                                "storyboard": storyboard(info)})
            elif url.path == "/api/stream" and VIDEO_ID.match(vid):
                self.stream(vid, int(qs.get("q") or 1080), float(qs.get("t") or 0), qs.get("lang", ""), qs.get("x") == "1", qs.get("web") == "1")
            elif url.path == "/api/audio" and VIDEO_ID.match(vid):
                self.proxy_audio(vid, qs.get("lang", ""), qs.get("web") == "1")
            elif url.path == "/api/captions" and VIDEO_ID.match(vid) and LANG.match(qs.get("lang", "")):
                text = shift_vtt(caption_text(vid, qs["lang"], qs.get("auto") == "1"), float(qs.get("offset") or 0))
                self.send(200, text.encode(), "text/vtt; charset=utf-8")
            elif url.path == "/api/channel" and CHANNEL_ID.match(vid) and qs.get("tab", "videos") in CHANNEL_TABS:
                self.send(200, channel(vid, qs.get("tab", "videos")))
            elif url.path == "/api/library" and qs.get("name") in LIBRARY:
                if cfg:
                    self.send(200, videos(feed(LIBRARY[qs["name"]], 200)))
                else:
                    self.send(200, tv_library(qs["name"]))
            elif url.path == "/api/playlists":
                self.send(200, playlists(feed("https://www.youtube.com/feed/playlists", 200)) if cfg else tv_playlists())
            elif url.path == "/api/playlist" and PLAYLIST_ID.match(vid):
                if not cfg and ytv_session() and any(p["id"] == vid for p in tv_playlists()):  # yours, maybe private
                    self.send(200, tv_playlist(vid))
                else:
                    self.send(200, playlist(vid))

            elif url.path == "/api/results" and qs.get("q", "").strip():
                self.send(200, results(qs["q"].strip()[:200], qs.get("sort", ""), qs.get("date", ""),
                                       qs.get("type", ""), qs.get("length", "")))
            elif url.path == "/api/msearch" and qs.get("q", "").strip():
                self.send(200, music_search(qs["q"].strip()[:200]))
            elif url.path == "/api/music-rows":
                self.send(200, MUSIC_ROWS)
            elif url.path == "/api/open-request":
                self.send(200, open_requests.pop(0) if open_requests else {})
            elif url.path == "/api/radio" and VIDEO_ID.match(vid):
                self.send(200, radio(vid))
            elif url.path == "/api/liked-music":
                self.send(200, songs(feed("https://music.youtube.com/playlist?list=LM", 500)) if cfg
                          else tv_liked_music() if ytv_session() else [])
            elif url.path == "/api/music-foryou":
                self.send(200, music_for_you())
            elif url.path == "/api/lyrics" and qs.get("track", "").strip():
                try:
                    self.send(200, lyrics(qs.get("artist", "")[:200], qs["track"][:200], qs.get("album", "")[:200],
                                          int(float(qs.get("duration") or 0))))
                except (OSError, ValueError):
                    self.send(404, {"error": "No lyrics found."})
            elif url.path == "/api/suggest":
                self.send(200, suggest(qs.get("q", "")[:100]) if qs.get("q", "").strip() else [])
            elif url.path == "/api/comments" and VIDEO_ID.match(vid):
                self.send(200, comments(vid))
            elif url.path == "/api/downloads":
                self.send(200, {"dir": str(DL_DIR), "items": dl_items(), "jobs": list(jobs.values())})
            elif url.path == "/api/file" and VIDEO_ID.match(vid) and qs.get("kind") in ("video", "audio"):
                path = dl_path(vid, qs["kind"])
                if not path:
                    return self.send(404, {"error": "not downloaded"})
                self.send_file(path, {".m4a": "audio/mp4", ".webm": "audio/webm" if qs["kind"] == "audio" else "video/webm",
                                      ".opus": "audio/ogg"}.get(path.suffix, "video/mp4"))
            else:
                self.send(404, {"error": "not found"})
        except AuthError as e:
            self.send(401 if str(e) in ("expired", "not signed in") else 502, {"error": str(e)})
        except Exception as e:  # yt-dlp/cookie failures: report to the UI instead of dropping the connection
            self.send(502, {"error": str(e)})

    def do_POST(self):
        url = urlparse(self.path)
        arg = parse_qs(url.query).get("id", [""])[0]
        if not self.trusted():
            return self.send(403, {"error": "forbidden"})
        # validated id only: it ends up on mpv's command line
        qs = parse_qs(url.query)
        q = qs.get("q", ["1080"])[0]
        # only known category names reach mpv's command line
        cats = lambda k: [c for c in qs.get(k, [""])[0].split(",") if c in SB_CATEGORIES]
        kind = qs.get("kind", [""])[0]
        start = qs.get("start", ["0"])[0]
        if url.path == "/api/play" and not shutil.which("mpv"):
            return self.send(400, {"error": "mpv isn't installed"})
        if url.path == "/api/play" and VIDEO_ID.match(arg) and q.isdigit() and re.fullmatch(r"\d+(\.\d+)?", start):
            play(arg, int(q), cats("skip"), cats("ask"), float(start))
        elif url.path == "/api/download" and VIDEO_ID.match(arg) and kind in ("video", "audio") and q.isdigit():
            if jobs.get(f"{arg}:{kind}", {}).get("status") not in (None, "error"):
                return self.send(409, {"error": "Already downloading."})
            threading.Thread(target=download, args=(arg, kind, int(q) or 1080, qs.get("web", [""])[0] == "1"), daemon=True).start()
        elif url.path == "/api/download-delete" and VIDEO_ID.match(arg) and kind in ("video", "audio"):
            delete_download(arg, kind)
            jobs.pop(f"{arg}:{kind}", None)
        elif url.path == "/api/download-dismiss" and VIDEO_ID.match(arg) and kind in ("video", "audio"):
            jobs.pop(f"{arg}:{kind}", None)  # forget a failed download; leaves any earlier copy alone
        elif url.path == "/api/open-downloads":
            DL_DIR.mkdir(parents=True, exist_ok=True)
            os.startfile(DL_DIR) if WINDOWS else subprocess.Popen(["xdg-open", str(DL_DIR)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif url.path == "/api/remote-sync":
            # the page reports what's playing and picks up the phone's commands
            try:
                remote["state"] = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 500_000)) or b"{}")
            except ValueError:
                pass
            cmds, remote["cmds"] = remote["cmds"], []
            return self.send(200, cmds)
        elif url.path == "/api/remote":
            on = parse_qs(url.query).get("on", [""])[0] == "1"
            update_data({"pref:remote": on})
            try:
                remote_enable(on)
            except OSError as e:
                return self.send(500, {"error": f"Couldn't open port {REMOTE_PORT}: {e}"})
            return self.send(200, remote_info())
        elif url.path == "/api/remote-forget":
            save_remote({"tokens": [], "code": f"{secrets.randbelow(10 ** 6):06d}"})
            return self.send(200, remote_info())
        elif url.path == "/api/data":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                changes = json.loads(self.rfile.read(n)) if 0 < n <= 5_000_000 else None
            except ValueError:
                changes = None
            if not isinstance(changes, dict) or not all(isinstance(k, str) and DATA_KEY.match(k) for k in changes):
                return self.send(400, {"error": "bad data"})
            update_data(changes)
        elif url.path == "/api/clear-cache":
            forget(""), video.cache_clear(), results.cache_clear(), comments.cache_clear(), _infos.clear()
            MUSIC_CACHE.unlink(missing_ok=True)
        elif url.path == "/api/update-ytdlp" and FROZEN:
            return self.send(500, {"error": "This packaged TubeView gets a newer yt-dlp with each TubeView release."})
        elif url.path == "/api/update-ytdlp":
            # new version is used after TubeView restarts (this process already imported the old one)
            r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "yt-dlp[default]"],
                               capture_output=True, text=True, timeout=600)
            if r.returncode:
                return self.send(500, {"error": (r.stderr or r.stdout)[-800:]})
            ver = subprocess.run([sys.executable, "-c", "import yt_dlp.version as v; print(v.__version__)"],
                                 capture_output=True, text=True).stdout.strip()
            return self.send(200, {"ok": True, "version": ver, "running": yt_dlp.version.__version__})
        elif url.path == "/api/signin":
            try:
                sign_in()
            except ValueError as e:
                return self.send(400, {"error": str(e)})
        elif url.path == "/api/signout":
            sign_out()
        elif url.path.startswith("/api/yt/"):
            return self.yt_post(url.path.removeprefix("/api/yt/"), qs)
        elif url.path == "/api/ytv/start":
            try:
                return self.send(200, ytv_start())
            except (OSError, AuthError) as e:
                return self.send(502, {"error": str(e)})
        elif url.path == "/api/ytv/signout":
            ytv_sign_out()
        else:
            return self.send(400, {"error": "bad request"})
        self.send(200, {"ok": True})

    def stream(self, vid, q, start, lang="", keep_times=False, web=False):
        info = play_info(vid)
        formats = pick_formats(info, q, lang, web)
        if not formats:
            return self.send(502, {"error": "No playable format for this video."})
        start = 0 if info.get("is_live") else max(0.0, min(start, (info.get("duration") or 1e9) - 1))
        p = subprocess.Popen(ffmpeg_cmd(formats, start, q == 0, keep_times), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **NO_WINDOW)
        # MediaSource wants full codec strings; YouTube labels its WebM VP9 just "vp9"
        codecs = [{"vp9": "vp09.00.51.08"}.get(c, c) for f in formats for c in (f.get("vcodec"), f.get("acodec")) if c and c != "none"]
        try:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("X-Codecs", ", ".join(dict.fromkeys(codecs)))  # for the player's MediaSource buffer
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            while chunk := p.stdout.read1(1 << 16):
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # player seeked, changed quality or closed
        finally:
            p.kill()
            p.wait()

    def yt_post(self, action, qs):
        """Like, Subscribe and Save to playlist, through the YouTube TV sign-in."""
        arg = lambda k: qs.get(k, [""])[0]
        try:
            if action == "subscribe" and CHANNEL_ID.match(arg("id")):
                tv_subscribe(arg("id"), arg("on") == "1")
            elif action == "rate" and VIDEO_ID.match(arg("id")):
                tv_rate(arg("id"), arg("like") == "1")
            elif action == "playlist-add" and VIDEO_ID.match(arg("id")) and PLAYLIST_ID.match(arg("pl")):
                tv_playlist_add(arg("pl"), arg("id"))
            else:
                return self.send(400, {"error": "bad request"})
        except AuthError as e:
            return self.send(401 if str(e) in ("expired", "not signed in") else 502, {"error": str(e)})
        self.send(200, {"ok": True})

    def proxy_audio(self, vid, lang="", web=False):
        """Audio-only mode: pass YouTube's M4A through as-is (with Range, so seeking is instant).
        Remuxing audio alone through ffmpeg produces streams Chromium refuses to decode."""
        formats = pick_formats(play_info(vid), 0, lang, web)
        if not formats:
            return self.send(502, {"error": "No audio format for this video."})
        f = formats[0]
        headers = dict(f.get("http_headers") or {})
        if self.headers.get("Range"):
            headers["Range"] = self.headers["Range"]
        try:
            r = urllib.request.urlopen(urllib.request.Request(f["url"], headers=headers), timeout=20)
        except urllib.error.HTTPError as e:
            return self.send(e.code, {"error": "YouTube refused the audio stream."})
        with r:
            self.send_response(r.status)
            for h in ("Content-Length", "Content-Range"):
                if r.headers.get(h):
                    self.send_header(h, r.headers[h])
            self.send_header("Content-Type", "audio/webm" if f.get("ext") == "webm" else "audio/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            try:
                while chunk := r.read(1 << 16):
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def send_file(self, path, ctype):
        """Serve a downloaded file with HTTP Range support, so the player can seek in it."""
        size = path.stat().st_size
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        start, end = 0, size - 1
        if m and (m[1] or m[2]):
            start, end = (int(m[1]), int(m[2]) if m[2] else size - 1) if m[1] else (max(0, size - int(m[2])), size - 1)
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0 and (chunk := f.read(min(1 << 16, left))):
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *a):
        pass


def open_window(url):
    """TubeView's window: in this process when PySide6 is importable (the packaged app), else via the system Python."""
    try:
        sys.path.insert(0, str(HERE))
        import window
    except ImportError as e:
        if FROZEN:  # the packaged app has no window.py file to hand to another Python: say why, and use the browser
            msg = f"TubeView's window couldn't load ({e}). A missing system library is the usual cause."
            print(msg)
            try:
                CONF_DIR.mkdir(parents=True, exist_ok=True)
                (CONF_DIR / "window-error.log").write_text(msg + "\n")
            except OSError:
                pass
            return 1
        return subprocess.run([shutil.which("python3", path="/usr/bin:/usr/local/bin") or "python3", str(HERE / "window.py"), url]).returncode
    return window.main(url)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--yt-dlp"]:  # packaged Linux app standing in for yt-dlp (for mpv)
        sys.exit(yt_dlp.main(sys.argv[2:]))
    assert VIDEO_ID.match("dQw4w9WgXcQ") and not VIDEO_ID.match("--foo=bar;x")
    assert CHANNEL_ID.match("UCuAXFkgsw1L7xaCfnd5JJOw") and not CHANNEL_ID.match("UC/../../etc/passwd_xxxx")
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found: install it (sudo pacman -S ffmpeg)")
    fake = {"formats": [{"protocol": "https", "vcodec": "avc1.64", "acodec": "none", "height": h, "url": str(h)} for h in (360, 720, 1080)]
                       + [{"protocol": "https", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 128, "url": "a"}]}
    assert [f["url"] for f in pick_formats(fake, 720)] == ["720", "a"] and heights(fake) == [1080, 720, 360]
    fake["formats"].insert(0, {"format_id": "140-drc", "protocol": "https", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 999, "url": "drc"})
    assert [f["url"] for f in pick_formats(fake, 0)] == ["a"]
    fake["formats"] += [{"protocol": "https", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 500, "url": "ru", "language": "ru"},
                        {"protocol": "https", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 128, "url": "en", "language": "en",
                         "language_preference": 10}]
    assert pick_formats(fake, 720)[1]["url"] == "en" and pick_formats(fake, 720, "ru")[1]["url"] == "ru"
    assert search_params(kind="live") == "EgJAAQ==" and search_params(sort="date") == "CAI="
    assert clock_seconds("1:02:03") == 3723 and clock_seconds("0:45") == 45 and count("1.2M views") == 1200000
    assert shift_vtt("WEBVTT\n\n00:00:05.000 --> 00:00:08.000\nhi\n\n00:01:05.500 --> 00:01:08.000\nyo", 60) == \
        "WEBVTT\n\n00:00:05.500 --> 00:00:08.000\nyo"
    url = f"http://127.0.0.1:{PORT}"
    # the packaged app opens its own window unless asked for the browser
    window = "--window" in sys.argv or (FROZEN and "--browser" not in sys.argv)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:  # already running (e.g. launched twice from the app menu): just show it
        print(f"TubeView is already running at {url}")
        if window:
            sys.exit(open_window(url))
        if "--no-browser" not in sys.argv:
            webbrowser.open(url)
        sys.exit()
    threading.Thread(target=auto_update_ytdlp, daemon=True).start()
    threading.Thread(target=prefetch_music, daemon=True).start()
    threading.Thread(target=upload_alerts, daemon=True).start()
    if read_data().get("pref:remote"):
        try:
            remote_enable(True)
        except OSError as e:
            print(f"Phone remote unavailable: {e}")
    print(f"TubeView running at {url}  (Ctrl+C to quit)")
    if window:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        # 0 = closed normally, negative = killed by a signal; only a real failure (e.g. no PySide6) falls back
        if open_window(url) <= 0:
            sys.exit()
        print("Couldn't open the TubeView window (is PySide6 installed?); opening it in your browser instead.")
        webbrowser.open(url)
        threading.Event().wait()
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
