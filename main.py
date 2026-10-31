import asyncio
import hashlib
import io
import json
import os
import random
import re
import shutil
import time
import unicodedata

import edge_tts
import flet as ft
import flet_audio as fta
import PyPDF2
from gtts import gTTS
from mutagen.mp3 import MP3

# ---------------- settings ----------------
VOICE = "en-GB-SoniaNeural"
RATE = "-7%"              # "-10%" slower, "+10%" faster
VOICE_TAG = f"{VOICE}|{RATE}"   # saved audio is rebuilt if you change voice or rate
PREFETCH_AHEAD = 40       # how many pages ahead to prepare
WORKERS = 2               # pages converted at the same time
RETRIES = 3               # tries per piece of text before giving up
REQUEST_TIMEOUT = 30      # seconds per try
RETRY_COOLDOWN = 20       # seconds before a failed page is tried again
MAX_AUTO_RETRIES = 5      # then wait until you tap that page yourself
CHUNK_CHARS = 2500        # long pages are sent in pieces
SKIP_SECONDS = 10         # seconds for the skip back / forward buttons
UP_NEXT_COUNT = 10        # cards in the "Up next" strip

COVER = 210






PALETTES = [
    ("#6C63FF", "#B3A8FF"), ("#8B5CF6", "#F472B6"), ("#0EA5E9", "#6C63FF"),
    ("#14B8A6", "#5EE0B0"), ("#F59E0B", "#EF4444"), ("#EC4899", "#8B5CF6"),
    ("#3B82F6", "#22D3EE"), ("#10B981", "#3B82F6"), ("#F97316", "#EC4899"),
    ("#7C3AED", "#2DD4BF"),
]
ART_ICONS = [
    ft.Icons.AUTO_STORIES_ROUNDED, ft.Icons.MENU_BOOK_ROUNDED, ft.Icons.LIGHTBULB_ROUNDED,
    ft.Icons.SELF_IMPROVEMENT_ROUNDED, ft.Icons.PSYCHOLOGY_ROUNDED, ft.Icons.SPA_ROUNDED,
    ft.Icons.WB_SUNNY_ROUNDED, ft.Icons.NIGHTLIGHT_ROUNDED, ft.Icons.FILTER_VINTAGE_ROUNDED,
    ft.Icons.EXPLORE_ROUNDED, ft.Icons.FLARE_ROUNDED, ft.Icons.BOLT_ROUNDED,
]


def art(seed, size, radius):
    """Gradient cover with soft circles and an icon. Same seed = same picture."""
    h = int(hashlib.md5(seed.encode()).hexdigest(), 16)
    c1, c2 = PALETTES[h % len(PALETTES)]
    icon = ART_ICONS[(h // 7) % len(ART_ICONS)]
    flip = (h // 3) % 2 == 0
    glow = ft.Colors.with_opacity(0.14, W)
    return ft.Container(
        width=size, height=size, border_radius=radius,
        clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
        gradient=ft.LinearGradient(
            begin=ft.Alignment.TOP_LEFT if flip else ft.Alignment.BOTTOM_LEFT,
            end=ft.Alignment.BOTTOM_RIGHT if flip else ft.Alignment.TOP_RIGHT,
            colors=[c1, c2],
        ),
        content=ft.Stack(
            [
                ft.Container(width=size * 0.9, height=size * 0.9, border_radius=size * 0.45,
                             bgcolor=glow, left=-size * 0.3, top=-size * 0.3),
                ft.Container(width=size * 0.7, height=size * 0.7, border_radius=size * 0.35,
                             bgcolor=glow, right=-size * 0.2, bottom=-size * 0.2),
                ft.Container(
                    content=ft.Icon(icon, size=size * 0.42,
                                    color=ft.Colors.with_opacity(0.92, W)),
                    width=size, height=size, alignment=ft.Alignment.CENTER,
                ),
            ],
            width=size, height=size,
        ),
    )
    
# ---------------- page pictures ----------------
# 120 different pictures. Every page gets one at random (saved with the book,
# so a page keeps its picture). Needs internet, same as the voice.
IMAGE_SEEDS = [f"sona-{n:03d}" for n in range(1, 121)]
DEFAULT_SEED = "sona-000"
FIT_COVER = (getattr(ft, "BoxFit", None) or getattr(ft, "ImageFit")).COVER


def cover_url(seed, size):
    return f"https://picsum.photos/seed/{seed}/{size}/{size}"


def make_covers(n):
    """One picture per page, shuffled so neighbouring pages rarely look alike."""
    out = []
    while len(out) < n:
        pool = IMAGE_SEEDS[:]
        random.shuffle(pool)
        out.extend(pool)
    return out[:n]


# ---------------- colours (midnight + violet) ----------------
W = ft.Colors.WHITE
BG = "#0B0819"
SURFACE = "#1A1636"
SURFACE_2 = "#251F4A"
HAIRLINE = "#2F2960"
TEXT = "#F4F1FF"
MUTED = "#A9A4C9"
DIM = "#6F6A93"
ACCENT = "#6C63FF"
ACCENT_2 = "#8B5CF6"
ACCENT_LIGHT = "#B3A8FF"
ACCENT_SOFT = ft.Colors.with_opacity(0.18, ACCENT)
ACCENT_LINE = ft.Colors.with_opacity(0.65, ACCENT)
MINT = "#5EE0B0"
MINT_SOFT = ft.Colors.with_opacity(0.16, MINT)
AMBER = "#F2C46D"
AMBER_SOFT = ft.Colors.with_opacity(0.15, AMBER)
ERR_RED = "#FF8A9B"
ERR_SOFT = ft.Colors.with_opacity(0.15, ERR_RED)
NEUTRAL_SOFT = ft.Colors.with_opacity(0.08, W)
GLASS_BTN = ft.Colors.with_opacity(0.14, W)

ACCENT_GRAD = ft.LinearGradient(
    begin=ft.Alignment.TOP_LEFT, end=ft.Alignment.BOTTOM_RIGHT, colors=[ACCENT, ACCENT_2],
)
BG_DARK = ft.LinearGradient(
    begin=ft.Alignment.TOP_CENTER, end=ft.Alignment.BOTTOM_CENTER,
    colors=["#17113F", "#0B0819", "#08060F"],
)
BG_PLAYER = ft.LinearGradient(
    begin=ft.Alignment.TOP_CENTER, end=ft.Alignment.BOTTOM_CENTER,
    colors=["#7A48E0", "#4A2398", "#1B0F42"],
)


# ---------------- storage (saved books) ----------------
_ROOT = None


def get_root():
    """Folder that survives app restarts (phone: app data folder, Mac: ~/.pdf_audiobook)."""
    global _ROOT
    if _ROOT is None:
        base = os.getenv("FLET_APP_STORAGE_DATA") or os.path.join(
            os.path.expanduser("~"), ".pdf_audiobook"
        )
        _ROOT = os.path.join(base, "library")
        os.makedirs(_ROOT, exist_ok=True)
    return _ROOT


def book_dir(book_id):
    return os.path.join(get_root(), book_id)


def audio_dir(book_id):
    return os.path.join(book_dir(book_id), "audio")


def audio_file(book_id, idx):
    return os.path.join(audio_dir(book_id), f"p{idx:04d}.mp3")


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def load_meta(book_id):
    return read_json(os.path.join(book_dir(book_id), "meta.json"))


def save_meta(meta):
    write_json(os.path.join(book_dir(meta["id"]), "meta.json"), meta)


def load_pages(book_id):
    return read_json(os.path.join(book_dir(book_id), "pages.json"), [])


def scan_audio(book_id):
    """Which pages already have saved audio: {page index: path}."""
    found = {}
    d = audio_dir(book_id)
    if not os.path.isdir(d):
        return found
    for name in os.listdir(d):
        m = re.fullmatch(r"p(\d+)\.mp3", name)
        if m:
            path = os.path.join(d, name)
            if os.path.getsize(path) > 0:
                found[int(m.group(1))] = path
    return found


def clear_audio(book_id):
    shutil.rmtree(audio_dir(book_id), ignore_errors=True)
    os.makedirs(audio_dir(book_id), exist_ok=True)


def list_books():
    books = []
    root = get_root()
    for name in os.listdir(root):
        meta = load_meta(name)
        if not meta:
            continue
        found = scan_audio(name)
        size = sum(os.path.getsize(p) for p in found.values() if os.path.exists(p))
        meta["audio_ready"] = len(found)
        meta["audio_bytes"] = size
        books.append(meta)
    books.sort(key=lambda m: m.get("last_opened", 0), reverse=True)
    return books


def import_pdf(data: bytes, name: str):
    """Parse once, save the text. Same PDF again = opens the saved copy."""
    book_id = hashlib.sha1(data).hexdigest()[:12]
    if load_meta(book_id) and os.path.exists(os.path.join(book_dir(book_id), "pages.json")):
        return book_id, False
    pages = extract_pages(data)
    if not pages:
        raise ValueError("No readable text found. Scanned (image-only) PDFs are not supported.")
    os.makedirs(audio_dir(book_id), exist_ok=True)
    write_json(os.path.join(book_dir(book_id), "pages.json"), pages)
    now = time.time()
    save_meta({
        "id": book_id, "name": name, "n_pages": len(pages),
        "last_page": 0, "added": now, "last_opened": now, "voice_tag": VOICE_TAG,
        "done": [], "covers": make_covers(len(pages)),
    })
    return book_id, True


def delete_book(book_id):
    shutil.rmtree(book_dir(book_id), ignore_errors=True)


# ---------------- text / pdf helpers ----------------
def clean_text(t: str) -> str:
    t = unicodedata.normalize("NFKC", t)                        # ligatures: fi -> fi
    t = "".join(
        ch for ch in t
        if ch in "\n\t " or unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co")
    )                                                           # control / invisible chars
    t = re.sub(r"(\w)-\s+(\w)", r"\1\2", t)                     # "exam- ple" -> "example"
    t = re.sub(r"[.\u2026\u00b7\u2022_=~*\-]{4,}", " ", t)      # dotted leaders, rules
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def extract_pages(data: bytes):
    reader = PyPDF2.PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            raise ValueError("This PDF is password protected.")
    pages = []
    for p in reader.pages:
        try:
            t = p.extract_text() or ""
        except Exception:
            t = ""                       # one bad page must not stop the whole book
        t = clean_text(t)
        if len(re.findall(r"[^\W_]", t)) >= 3:
            pages.append(t)
    return pages


def split_text(text, limit=CHUNK_CHARS):
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks, cur = [], ""
    for s in sentences:
        while len(s) > limit:                 # one giant "sentence"
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(s[:limit])
            s = s[limit:]
        if len(cur) + len(s) + 1 > limit and cur:
            chunks.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        chunks.append(cur)
    return chunks or [text]


def fmt_time(sec):
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def short(text, n=420):
    text = text.strip()
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + "..."


NET_ERR = "No internet - can't reach the voice server. Retrying..."


def err_text(err, n=110):
    msg = str(err).strip()
    low = msg.lower()
    if any(k in low for k in ("cannot connect to host", "dns", "getaddrinfo",
                              "name or service", "network is unreachable")):
        return NET_ERR
    name = type(err).__name__
    return f"{name}: {msg}"[:n] if msg else name


def bar_controls(frac, color=MINT, h=4):
    """A thin progress bar made of two flexible pieces (fill + track)."""
    frac = max(0.0, min(1.0, frac))
    r = h / 2
    if frac <= 0:
        return [ft.Container(expand=1, height=h, bgcolor=HAIRLINE, border_radius=r)]
    if frac >= 1:
        return [ft.Container(expand=1, height=h, bgcolor=color, border_radius=r)]
    a = max(1, min(999, int(frac * 1000)))
    return [
        ft.Container(expand=a, height=h, bgcolor=color, border_radius=r),
        ft.Container(expand=1000 - a, height=h, bgcolor=HAIRLINE, border_radius=r),
    ]


def thumb(seed, size, radius, opacity=1.0):
    c = art(seed, size, radius)
    c.opacity = opacity
    return c


# ---------------- speech helpers ----------------
async def fetch_chunk(chunk):
    comm = edge_tts.Communicate(chunk, VOICE, rate=RATE)
    data = bytearray()
    async for item in comm.stream():
        if item["type"] == "audio":
            data += item["data"]
    if not data:
        raise RuntimeError("no audio received")
    return bytes(data)


async def edge_page(text, path):
    with open(path, "wb") as f:
        for chunk in split_text(text):
            if not re.search(r"\w", chunk):
                continue
            last = None
            for attempt in range(RETRIES):
                try:
                    # timeout is per piece, so long pages are not cut off
                    f.write(await asyncio.wait_for(fetch_chunk(chunk), REQUEST_TIMEOUT))
                    break
                except Exception as err:
                    last = err
                    await asyncio.sleep(2 * (attempt + 1))
            else:
                raise last
    if os.path.getsize(path) == 0:
        raise RuntimeError("nothing readable on this page")


def gtts_to_file(text, path):
    gTTS(text=text, lang="en").save(path)


async def synthesize(text, path, allow_fallback):
    """Neural voice with retries. The basic gTTS voice is used only as a last
    resort, and only when you are waiting for that page."""
    text = clean_text(text)
    try:
        await edge_page(text, path)
        return "edge"
    except Exception as e1:
        edge_err = err_text(e1)
        if not allow_fallback:
            raise RuntimeError(f"Edge voice failed ({edge_err})")
        try:
            await asyncio.wait_for(asyncio.to_thread(gtts_to_file, text, path), 90)
            return "gtts"
        except Exception as e2:
            raise RuntimeError(f"Edge: {edge_err} | gTTS: {err_text(e2, 60)}")


def mp3_duration(path):
    try:
        return MP3(path).info.length
    except Exception:
        return None


# ---------------- app ----------------
def main(page: ft.Page):
    page.title = "PDF Audiobook"
    page.theme_mode = ft.ThemeMode.DARK
    page.bgcolor = BG
    page.padding = 0

    state = {
        "book_id": None,
        "meta": None,
        "pages": [],
        "covers": [],      # picture seed per page
        "index": 0,
        "audio": None,
        "playing": False,
        "name": "",
        "token": 0,        # invalidates stale playback
        "session": 0,      # changes when another book is opened
        "cache": {},       # page index -> saved mp3 path
        "inflight": {},    # page index -> running task
        "failed": {},      # page index -> time of last failure
        "errors": {},      # page index -> what went wrong
        "attempts": {},    # page index -> failed tries so far
        "bg": [],
        "pos": 0.0,        # seconds into the current page
        "dur": 0.0,        # length of the current page audio (seconds)
        "dragging": False,
        "view": "player",
        "done": set(),     # pages listened to the end (saved in meta.json)
        "filter": "all",   # all | new | done | ready
        "up_sig": None,    # which pages the "Up next" strip shows
    }
    rows = []          # one dict per page in "All pages"
    row_last = []      # last drawn state per row, to skip needless updates
    nav_refs = {}      # name -> (container, icon)
    chip_refs = {}     # filter key -> (container, text)

    def seed_of(i):
        c = state["covers"]
        return c[i] if 0 <= i < len(c) else DEFAULT_SEED

    # ---------- controls: player view ----------
    head_sub = ft.Text("No PDF loaded", size=12, color=ft.Colors.with_opacity(0.75, W),
                       text_align=ft.TextAlign.CENTER)
    ring = ft.ProgressRing(visible=False, width=18, height=18, stroke_width=2, color=W)

    cover_img = ft.Container(content=art(DEFAULT_SEED, COVER, 0), width=COVER, height=COVER)
    cover_sub = ft.Text("Open a PDF to begin", size=13, color=ft.Colors.with_opacity(0.75, W))
    cover_badge = ft.Container(
        content=ft.Row(
            [ft.Icon(ft.Icons.DONE_ALL_ROUNDED, size=14, color=MINT),
             ft.Text("Listened", size=12, weight=ft.FontWeight.W_600, color=MINT)],
            spacing=6, tight=True,
        ),
        padding=ft.Padding(left=10, top=4, right=10, bottom=4),
        border_radius=14, bgcolor=ft.Colors.with_opacity(0.28, "#000000"), visible=False,
    )
    title_text = ft.Text("Your audiobook", size=20, weight=ft.FontWeight.W_700, color=W,
                     max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
    status = ft.Text("Add a PDF from the Library", size=13,
                 color=ft.Colors.with_opacity(0.8, W),
                 max_lines=2, overflow=ft.TextOverflow.ELLIPSIS)
    preview = ft.Text("", size=12, color=ft.Colors.with_opacity(0.65, W), max_lines=2,
                  overflow=ft.TextOverflow.ELLIPSIS)
    prep_err = ft.Text("", size=11, color=ERR_RED, visible=False, max_lines=3,
                       overflow=ft.TextOverflow.ELLIPSIS)

    seek_slider = ft.Slider(
        min=0, max=1, value=0, disabled=True,
        active_color=ACCENT_LIGHT, inactive_color=ft.Colors.with_opacity(0.22, W), thumb_color=W,
    )
    pos_text = ft.Text("0:00", size=12, color=ft.Colors.with_opacity(0.75, W))
    dur_text = ft.Text("0:00", size=12, color=ft.Colors.with_opacity(0.75, W))

    glass_style = ft.ButtonStyle(shape=ft.CircleBorder(), bgcolor=GLASS_BTN)
    play_btn = ft.IconButton(icon=ft.Icons.PLAY_ARROW_ROUNDED, icon_size=40, icon_color=W)
    prev_btn = ft.IconButton(icon=ft.Icons.SKIP_PREVIOUS_ROUNDED, icon_size=28, icon_color=W,
                             tooltip="Previous page", style=glass_style)
    next_btn = ft.IconButton(icon=ft.Icons.SKIP_NEXT_ROUNDED, icon_size=28, icon_color=W,
                             tooltip="Next page", style=glass_style)
    back_btn = ft.IconButton(icon=ft.Icons.REPLAY_10_ROUNDED, icon_size=22,
                             icon_color=ft.Colors.with_opacity(0.85, W),
                             tooltip=f"Back {SKIP_SECONDS} seconds", style=glass_style)
    fwd_btn = ft.IconButton(icon=ft.Icons.FORWARD_10_ROUNDED, icon_size=22,
                            icon_color=ft.Colors.with_opacity(0.85, W),
                            tooltip=f"Forward {SKIP_SECONDS} seconds", style=glass_style)

    # ---------- controls: pages view ----------
    pages_title = ft.Text("All pages", size=24, weight=ft.FontWeight.W_800, color=TEXT,
                          max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
    stat_num = ft.Text("0", size=34, weight=ft.FontWeight.W_800, color=MINT)
    stat_of = ft.Text("of 0 pages listened", size=14, color=MUTED)
    stat_bar = ft.Row(bar_controls(0), spacing=0)
    pages_sub = ft.Text("No book open", size=12, color=DIM)
    page_input = ft.TextField(
        label="Page", width=92, dense=True, text_align=ft.TextAlign.CENTER,
        keyboard_type=ft.KeyboardType.NUMBER, color=TEXT, bgcolor=SURFACE,
        border_color=HAIRLINE, focused_border_color=ACCENT, border_radius=14,
    )
    hint_text = ft.Text("Tap a page to play  ·  hold to mark it listened or not",
                        size=11, color=DIM)
    pages_empty = ft.Text("No book open. Choose one from the Library.", size=14, color=MUTED)
    up_strip = ft.Row([], spacing=14, scroll=ft.ScrollMode.AUTO)
    upnext_section = ft.Column(
        [ft.Text("Up next", size=17, weight=ft.FontWeight.W_700, color=TEXT), up_strip],
        spacing=10, visible=False,
    )
    pages_list = ft.ListView(controls=[], spacing=8, expand=True)

    # ---------- controls: library view ----------
    library_sub = ft.Text("", size=13, color=MUTED)
    library_list = ft.ListView(controls=[], spacing=10, expand=True)
    library_empty = ft.Text("Nothing saved yet. Tap Add PDF to start your library.",
                            size=14, color=MUTED)

    file_picker = ft.FilePicker()
    page.services.append(file_picker)

    # ---------- ui helpers ----------
    def set_play_icon(playing: bool):
        play_btn.icon = ft.Icons.PAUSE_ROUNDED if playing else ft.Icons.PLAY_ARROW_ROUNDED

    def set_busy(busy: bool):
        ring.visible = busy
        play_btn.disabled = busy
        page.update()

    def refresh_progress():
        pos = min(state["pos"], state["dur"]) if state["dur"] else state["pos"]
        pos_text.value = fmt_time(pos)
        if not state["dragging"] and state["dur"]:
            seek_slider.value = max(0, min(pos, state["dur"]))
        page.update()

    def reset_progress(duration=0.0):
        state["pos"] = 0.0
        state["dur"] = duration or 0.0
        seek_slider.value = 0
        if duration:
            seek_slider.max = duration
            seek_slider.disabled = False
        else:
            seek_slider.disabled = True
        pos_text.value = "0:00"
        dur_text.value = fmt_time(duration or 0)

    def row_status(i):
        if i == state["index"]:
            return "current"
        if i in state["done"]:
            return "done"
        if i in state["cache"]:
            return "ready"
        if i in state["inflight"]:
            return "loading"
        if i in state["failed"]:
            return "failed"
        return "wait"

    # status -> (icon, icon colour, badge bg, row bg, row border, label, label colour)
    ROW_STYLE = {
        "current": (ft.Icons.GRAPHIC_EQ_ROUNDED, W, ACCENT, SURFACE_2, ACCENT_LINE,
                    "Now playing", ACCENT_LIGHT),
        "done": (ft.Icons.CHECK_ROUNDED, MINT, MINT_SOFT, SURFACE, HAIRLINE,
                 "Listened", MINT),
        "ready": (ft.Icons.PLAY_ARROW_ROUNDED, TEXT, NEUTRAL_SOFT, SURFACE, HAIRLINE,
                  "Audio ready", MUTED),
        "loading": (ft.Icons.HOURGLASS_TOP_ROUNDED, AMBER, AMBER_SOFT, SURFACE, HAIRLINE,
                    "Preparing audio", AMBER),
        "failed": (ft.Icons.ERROR_OUTLINE_ROUNDED, ERR_RED, ERR_SOFT, SURFACE, HAIRLINE,
                   "Failed", ERR_RED),
        "wait": (ft.Icons.SCHEDULE_ROUNDED, DIM, NEUTRAL_SOFT, SURFACE, HAIRLINE,
                 "Queued", DIM),
    }

    def passes_filter(i):
        f = state["filter"]
        if f == "all" or i == state["index"]:
            return True
        if f == "new":
            return i not in state["done"]
        if f == "done":
            return i in state["done"]
        if f == "ready":
            return i in state["cache"]
        return True

    def refresh_rows():
        for i, r in enumerate(rows):
            s = row_status(i)
            err = state["errors"].get(i, "") if s == "failed" else ""
            is_done = i in state["done"]
            key = (s, state["filter"], err, is_done)
            if row_last[i] == key:
                continue
            row_last[i] = key
            icon_name, icon_col, badge_bg, row_bg, row_border, label, label_col = ROW_STYLE[s]
            r["icon"].icon, r["icon"].color = icon_name, icon_col
            r["badge"].bgcolor = badge_bg
            r["cont"].bgcolor = row_bg
            r["cont"].border = ft.Border.all(1, row_border)
            r["cont"].visible = passes_filter(i)
            r["thumb"].opacity = 0.5 if (is_done and s != "current") else 1.0
            r["title"].color = MUTED if (is_done and s != "current") else TEXT
            if err:
                label = f"Failed: {err} (tap to retry)"
            elif s == "current" and is_done:
                label = "Now playing  ·  listened"
            r["label"].value = f"Page {i + 1}  ·  {label}"
            r["label"].color = label_col

    def build_rows():
        rows.clear()
        row_last.clear()
        state["up_sig"] = None
        row_controls = []
        for i, text in enumerate(state["pages"]):
            th = thumb(seed_of(i), 56, 16)
            title = ft.Text(short(text, 70), size=14, weight=ft.FontWeight.W_600, color=TEXT,
                            max_lines=2, overflow=ft.TextOverflow.ELLIPSIS)
            label = ft.Text("", size=11, weight=ft.FontWeight.W_500, color=DIM,
                            max_lines=2, overflow=ft.TextOverflow.ELLIPSIS)
            icon = ft.Icon(ft.Icons.SCHEDULE_ROUNDED, size=20, color=DIM)
            badge = ft.Container(
                content=icon, width=40, height=40, border_radius=14,
                alignment=ft.Alignment.CENTER, bgcolor=NEUTRAL_SOFT,
            )
            cont = ft.Container(
                content=ft.Row(
                    [th, ft.Column([title, label], spacing=4, expand=True), badge],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=12,
                ),
                padding=ft.Padding(left=10, top=10, right=12, bottom=10),
                border_radius=20, bgcolor=SURFACE,
                border=ft.Border.all(1, HAIRLINE),
                ink=True, on_click=row_tap(i), on_long_press=row_hold(i),
            )
            rows.append({"cont": cont, "icon": icon, "badge": badge, "thumb": th,
                         "title": title, "label": label})
            row_last.append(None)
            row_controls.append(cont)
        pages_list.controls = [pages_header] + row_controls
        pages_empty.visible = not row_controls
        refresh_rows()
        refresh_upnext()

    def refresh_upnext():
        n = len(state["pages"])
        ids = [i for i in range(state["index"] + 1, n) if i not in state["done"]][:UP_NEXT_COUNT]
        if ids == state["up_sig"]:
            return
        state["up_sig"] = ids
        cards = []
        for i in ids:
            cards.append(ft.Container(
                content=ft.Column(
                    [
                        thumb(seed_of(i), 128, 22),
                        ft.Text(f"Page {i + 1}", size=13, weight=ft.FontWeight.W_700, color=TEXT),
                        ft.Text(short(state["pages"][i], 40), size=11, color=MUTED, max_lines=2,
                                overflow=ft.TextOverflow.ELLIPSIS),
                    ],
                    spacing=5,
                ),
                width=128, ink=True, border_radius=22, on_click=row_tap(i),
            ))
        up_strip.controls = cards
        upnext_section.visible = bool(cards)

    def refresh_ui():
        pages, i = state["pages"], state["index"]
        n = len(pages)
        if n:
            title_text.value = state["name"]
            pages_title.value = state["name"]
            cover_img.src = cover_url(seed_of(i), 700)
            cover_sub.value = f"Page {i + 1} of {n}"
            cover_badge.visible = i in state["done"]
            cover_img.content = art(seed_of(i), COVER, 0)
            preview.value = short(pages[i], 110)
            page_input.value = str(i + 1)
            page_input.error_text = None
            refresh_rows()
            refresh_upnext()
        page.update()

    def refresh_prep():
        n = len(state["pages"])
        if not n:
            return
        listened = len(state["done"])
        head_sub.value = f"{listened} of {n} listened  ·  audio ready {len(state['cache'])}/{n}"
        stat_num.value = str(listened)
        stat_of.value = f"of {n} pages listened"
        stat_bar.controls = bar_controls(listened / n)
        sub = f"Audio ready for {len(state['cache'])} of {n} pages"
        if state["failed"]:
            sub += f"  ·  {len(state['failed'])} failed"
        pages_sub.value = sub
        cover_badge.visible = state["index"] in state["done"]
        if state["failed"]:
            idx = max(state["failed"], key=state["failed"].get)
            prep_err.value = f"Page {idx + 1}: {state['errors'].get(idx, 'unknown problem')}"
            prep_err.visible = True
        else:
            prep_err.visible = False
        refresh_rows()
        refresh_upnext()
        page.update()

    def switch_view(name):
        state["view"] = name
        player_view.visible = name == "player"
        pages_view.visible = name == "pages"
        library_view.visible = name == "library"
        bg_box.gradient = BG_PLAYER if name == "player" else BG_DARK
        for key, (cont, icon) in nav_refs.items():
            on = key == name
            cont.gradient = ACCENT_GRAD if on else None
            icon.color = W if on else DIM
        page.update()

    def style_chips():
        for key, (cont, txt) in chip_refs.items():
            on = key == state["filter"]
            cont.bgcolor = ACCENT if on else SURFACE
            cont.border = ft.Border.all(1, ACCENT if on else HAIRLINE)
            txt.color = W if on else MUTED
            txt.weight = ft.FontWeight.W_700 if on else ft.FontWeight.W_500

    # ---------- library list ----------
    def refresh_library():
        books = list_books()
        controls = []
        for meta in books:
            current = meta["id"] == state["book_id"]
            n = max(1, meta["n_pages"])
            listened = len(state["done"]) if current else len(meta.get("done", []))
            covers = meta.get("covers") or [DEFAULT_SEED]
            mb = meta.get("audio_bytes", 0) / (1024 * 1024)
            sub = (f"{meta['n_pages']} pages  ·  on page {meta.get('last_page', 0) + 1}  ·  "
                   f"{mb:.0f} MB audio")
            controls.append(ft.Container(
                content=ft.Column(
                    [
                        ft.Row(
                            [
                                thumb(covers[0], 56, 16),
                                ft.Column(
                                    [
                                        ft.Text(meta["name"], size=15, weight=ft.FontWeight.W_700,
                                                color=TEXT, max_lines=1,
                                                overflow=ft.TextOverflow.ELLIPSIS),
                                        ft.Text(sub, size=12, color=MUTED, max_lines=2,
                                                overflow=ft.TextOverflow.ELLIPSIS),
                                    ],
                                    spacing=3, expand=True,
                                ),
                                ft.IconButton(icon=ft.Icons.DELETE_OUTLINE_ROUNDED,
                                              icon_color=DIM, tooltip="Delete this book",
                                              on_click=delete_tap(meta)),
                            ],
                            vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=12,
                        ),
                        ft.Container(content=ft.Row(bar_controls(listened / n), spacing=0),
                                     padding=ft.Padding(left=0, top=0, right=8, bottom=0)),
                        ft.Text(f"{listened} of {meta['n_pages']} pages listened", size=11,
                                color=MINT if listened else DIM),
                    ],
                    spacing=8,
                ),
                padding=ft.Padding(left=10, top=10, right=4, bottom=12),
                border_radius=22, bgcolor=SURFACE_2 if current else SURFACE,
                border=ft.Border.all(1, ACCENT_LINE if current else HAIRLINE),
                ink=True, on_click=book_tap(meta["id"]),
            ))
        library_list.controls = controls
        library_empty.visible = not controls
        total = sum(m.get("audio_bytes", 0) for m in books) / (1024 * 1024)
        library_sub.value = (f"{len(books)} saved  ·  {total:.0f} MB of audio on this device"
                             if books else "")
        page.update()

    # ---------- audio cache / prefetch ----------
    async def _generate(idx, session, book_id, text, allow_fallback):
        os.makedirs(audio_dir(book_id), exist_ok=True)
        final = audio_file(book_id, idx)
        part = final + ".part"
        try:
            await synthesize(text, part, allow_fallback)
            os.replace(part, final)
            if session == state["session"]:
                state["cache"][idx] = final
            return final
        except BaseException:
            try:
                os.remove(part)
            except OSError:
                pass
            raise
        finally:
            if session == state["session"]:
                state["inflight"].pop(idx, None)

    async def get_audio(idx, urgent=False):
        cached = state["cache"].get(idx)
        if cached and os.path.exists(cached):
            return cached
        task = state["inflight"].get(idx)
        if task is None:
            task = asyncio.create_task(_generate(
                idx, state["session"], state["book_id"], state["pages"][idx], urgent,
            ))
            state["inflight"][idx] = task
            refresh_rows()
        return await asyncio.shield(task)

    async def prefetch_worker(session):
        while session == state["session"]:
            n = len(state["pages"])
            start = state["index"]
            end = min(n, start + PREFETCH_AHEAD)
            now = time.time()
            idx = next(
                (i for i in range(start, end)
                 if i not in state["cache"]
                 and i not in state["inflight"]
                 and state["attempts"].get(i, 0) < MAX_AUTO_RETRIES
                 and now - state["failed"].get(i, 0) > RETRY_COOLDOWN),
                None,
            )
            if idx is None:
                await asyncio.sleep(1)
                continue
            try:
                await get_audio(idx)
                state["failed"].pop(idx, None)
                state["errors"].pop(idx, None)
                state["attempts"].pop(idx, None)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                state["failed"][idx] = time.time()
                state["errors"][idx] = err_text(err)
                if err_text(err) != NET_ERR:          # no internet = keep retrying
                    state["attempts"][idx] = state["attempts"].get(idx, 0) + 1
            if session == state["session"]:
                refresh_prep()
            await asyncio.sleep(0.2)

    def start_prefetch():
        for t in state["bg"]:
            t.cancel()
        state["bg"] = [
            asyncio.create_task(prefetch_worker(state["session"]))
            for _ in range(WORKERS)
        ]

    def reset_memory():
        """Forget in-memory progress of the current book. Saved files are kept."""
        state["session"] += 1
        for t in state["bg"]:
            t.cancel()
        for t in list(state["inflight"].values()):
            t.cancel()
        state["cache"] = {}
        state["inflight"] = {}
        state["failed"] = {}
        state["errors"] = {}
        state["attempts"] = {}
        state["bg"] = []

    def save_progress():
        meta = state["meta"]
        if not meta:
            return
        meta["last_page"] = state["index"]
        meta["last_opened"] = time.time()
        meta["done"] = sorted(state["done"])
        try:
            save_meta(meta)
        except Exception:
            pass

    # ---------- opening / closing books ----------
    def clear_book_ui():
        state.update(book_id=None, meta=None, pages=[], covers=[], index=0, name="",
                     done=set())
        build_rows()
        title_text.value = "Your audiobook"
        pages_title.value = "All pages"
        pages_sub.value = "No book open"
        stat_num.value = "0"
        stat_of.value = "of 0 pages listened"
        stat_bar.controls = bar_controls(0)
        cover_img.src = cover_url(DEFAULT_SEED, 700)
        cover_sub.value = "Open a PDF to begin"
        cover_img.content = art(DEFAULT_SEED, COVER, 0)
        cover_badge.visible = False
        preview.value = ""
        head_sub.value = "No PDF loaded"
        status.value = "Add a PDF from the Library"
        prep_err.visible = False
        reset_progress()
        set_play_icon(False)

    async def open_book(book_id, view="pages"):
        await stop_audio()
        reset_memory()
        state["token"] += 1
        status.value = "Opening..."
        set_busy(True)
        try:
            meta = load_meta(book_id)
            pages = await asyncio.to_thread(load_pages, book_id)
            if not meta or not pages:
                status.value = "This saved book could not be opened."
                set_busy(False)
                return
            if meta.get("voice_tag") != VOICE_TAG:       # voice/rate changed: rebuild audio
                await asyncio.to_thread(clear_audio, book_id)
                meta["voice_tag"] = VOICE_TAG
            covers = meta.get("covers")
            if not covers or len(covers) != len(pages):  # older books get pictures too
                covers = make_covers(len(pages))
                meta["covers"] = covers
            state["book_id"] = book_id
            state["meta"] = meta
            state["pages"] = pages
            state["covers"] = covers
            state["name"] = meta["name"]
            state["index"] = max(0, min(int(meta.get("last_page", 0)), len(pages) - 1))
            state["cache"] = await asyncio.to_thread(scan_audio, book_id)
            state["done"] = {int(i) for i in meta.get("done", []) if 0 <= int(i) < len(pages)}
            save_progress()
            reset_progress()
            build_rows()
            refresh_ui()
            refresh_prep()
            start_prefetch()
            status.value = f"Ready. Press play to continue from page {state['index'] + 1}."
        except Exception as err:
            status.value = f"Could not open this book: {err_text(err)}"
        set_busy(False)
        if view == "library":
            refresh_library()
        switch_view(view)

    def book_tap(book_id):
        async def handler(e):
            await open_book(book_id, view="pages")
        return handler

    def delete_tap(meta):
        async def handler(e):
            def close(_=None):
                page.pop_dialog()

            async def confirm(_):
                page.pop_dialog()
                if meta["id"] == state["book_id"]:
                    await stop_audio()
                    reset_memory()
                    state["token"] += 1
                    clear_book_ui()
                await asyncio.to_thread(delete_book, meta["id"])
                refresh_library()

            page.show_dialog(ft.AlertDialog(
                modal=True,
                title=ft.Text("Delete this book?"),
                content=ft.Text(f"{meta['name']}\n\nThe saved text, audio and listening "
                                f"progress will be removed from this device."),
                actions=[
                    ft.TextButton("Cancel", on_click=close),
                    ft.TextButton("Delete", on_click=confirm),
                ],
            ))
        return handler

    # ---------- playback ----------
    async def stop_audio():
        a = state["audio"]
        state["audio"] = None
        state["playing"] = False
        if a is not None:
            try:
                await a.pause()
            except Exception:
                pass
            try:
                page.services.remove(a)
            except ValueError:
                pass
            page.update()
            await asyncio.sleep(0.15)   # let the old player fully unmount
        set_play_icon(False)

    async def advance(my_token, finished=True):
        if my_token != state["token"]:      # already moved on
            return
        state["token"] += 1                 # stops tracker + "completed" from both firing
        if finished:
            state["done"].add(state["index"])
            save_progress()
            refresh_prep()
        state["playing"] = False
        if state["index"] < len(state["pages"]) - 1:
            status.value = f"Page {state['index'] + 1} finished"
            state["index"] += 1
            await play_current()
        else:
            set_play_icon(False)
            status.value = "Finished the last page"
            page.update()

    async def tracker(my_token, duration):
        """Moves the progress bar and starts the next page at the end.
        Backup for the audio 'completed' event, which is not reliable on Android."""
        tick = 0
        while my_token == state["token"]:
            await asyncio.sleep(0.25)
            if state["playing"]:
                state["pos"] += 0.25
                if state["pos"] >= duration + 0.8:
                    await advance(my_token)
                    return
                tick += 1
                if tick % 2 == 0:
                    refresh_progress()

    async def play_current():
        if not state["pages"]:
            return
        await stop_audio()
        state["token"] += 1
        my_token = state["token"]
        idx = state["index"]
        reset_progress()
        save_progress()
        refresh_ui()

        if idx not in state["cache"]:
            status.value = f"Preparing page {idx + 1}..."
            set_busy(True)

        state["failed"].pop(idx, None)
        state["attempts"].pop(idx, None)
        try:
            try:
                path = await get_audio(idx, urgent=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                # a background try may have failed just before; try once more,
                # this time allowed to use the basic voice as a last resort
                path = await get_audio(idx, urgent=True)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            msg = err_text(err, 200)
            state["failed"][idx] = time.time()
            state["errors"][idx] = msg
            status.value = f"Page {idx + 1} failed: {msg}"
            set_busy(False)
            refresh_rows()
            return
        state["errors"].pop(idx, None)

        if my_token != state["token"]:      # user skipped meanwhile
            return

        duration = mp3_duration(path) or 0.0

        async def on_done(e, t=my_token):
            if str(e.state).lower().endswith("completed"):
                await advance(t)

        def on_pos(e, t=my_token):
            if t == state["token"]:
                state["pos"] = e.position / 1000.0

        audio = fta.Audio(
            src=path, autoplay=False,
            on_state_change=on_done, on_position_change=on_pos,
        )
        state["audio"] = audio
        page.services.append(audio)
        page.update()
        await asyncio.sleep(0.4)            # let the new player mount

        if my_token != state["token"]:      # user skipped meanwhile
            return
        try:
            await audio.play()
        except Exception:
            await asyncio.sleep(0.6)        # one retry if it wasn't ready yet
            await audio.play()

        state["playing"] = True
        set_play_icon(True)
        reset_progress(duration)
        status.value = f"Playing page {idx + 1}"
        set_busy(False)
        refresh_rows()

        if duration:
            asyncio.create_task(tracker(my_token, duration))

    # ---------- handlers ----------
    async def pick_pdf(e):
        files = await file_picker.pick_files(allowed_extensions=["pdf"], with_data=True)
        if not files:
            return
        f = files[0]
        await stop_audio()
        status.value = "Reading the PDF (only needed once)..."
        switch_view("player")
        set_busy(True)
        try:
            if f.path:
                data = await asyncio.to_thread(lambda: open(f.path, "rb").read())
            else:
                data = f.bytes
            book_id, is_new = await asyncio.to_thread(import_pdf, data, f.name)
        except Exception as err:
            status.value = f"Could not read this PDF: {err_text(err, 160)}"
            set_busy(False)
            return
        set_busy(False)
        await open_book(book_id, view="pages")
        status.value = ("Saved to your library. Choose a page to play." if is_new
                        else "Already in your library. Choose a page to play.")
        page.update()

    async def play_click(e):
        if not state["pages"]:
            return
        a = state["audio"]
        if a is not None and state["playing"]:
            await a.pause()
            state["playing"] = False
            set_play_icon(False)
            status.value = "Paused"
            page.update()
        elif a is not None:
            await a.resume()
            state["playing"] = True
            set_play_icon(True)
            status.value = f"Playing page {state['index'] + 1}"
            page.update()
        else:
            await play_current()

    async def next_click(e):
        if state["pages"] and state["index"] < len(state["pages"]) - 1:
            state["index"] += 1
            await play_current()

    async def prev_click(e):
        if state["pages"] and state["index"] > 0:
            state["index"] -= 1
            await play_current()

    async def go_to_page(number):
        n = len(state["pages"])
        if not n:
            return
        if number < 1 or number > n:
            page_input.error_text = f"1 to {n}"
            page.update()
            return
        state["index"] = number - 1
        switch_view("player")
        await play_current()

    async def go_click(e):
        try:
            number = int(page_input.value)
        except (TypeError, ValueError):
            page_input.error_text = "Enter a number"
            page.update()
            return
        await go_to_page(number)

    def row_tap(i):
        async def handler(e):
            await go_to_page(i + 1)
        return handler

    def row_hold(i):
        """Long-press a page to flip its listened mark by hand."""
        async def handler(e):
            if i in state["done"]:
                state["done"].discard(i)
            else:
                state["done"].add(i)
            save_progress()
            row_last[i] = None
            state["up_sig"] = None
            refresh_prep()
        return handler

    async def seek_to(seconds):
        a = state["audio"]
        if a is None or not state["dur"]:
            return
        seconds = max(0.0, seconds)
        if seconds >= state["dur"] - 0.5:       # reached the end (skip or drag)
            await advance(state["token"], finished=True)
            return
        await a.seek(ft.Duration(milliseconds=int(seconds * 1000)))
        state["pos"] = seconds
        refresh_progress()

    async def back_click(e):
        await seek_to(state["pos"] - SKIP_SECONDS)

    async def fwd_click(e):
        await seek_to(state["pos"] + SKIP_SECONDS)

    def seek_drag_start(e):
        state["dragging"] = True

    def seek_dragging(e):
        pos_text.value = fmt_time(e.control.value)
        page.update()

    async def seek_drag_end(e):
        state["dragging"] = False
        await seek_to(float(e.control.value))

    def chip_tap(key):
        async def handler(e):
            state["filter"] = key
            style_chips()
            for i in range(len(row_last)):
                row_last[i] = None
            refresh_rows()
            page.update()
        return handler

    def nav_tap(name):
        async def handler(e):
            if name == "pages":
                refresh_rows()
            elif name == "library":
                refresh_library()
            switch_view(name)
        return handler

    play_btn.on_click = play_click
    prev_btn.on_click = prev_click
    next_btn.on_click = next_click
    back_btn.on_click = back_click
    fwd_btn.on_click = fwd_click
    seek_slider.on_change_start = seek_drag_start
    seek_slider.on_change = seek_dragging
    seek_slider.on_change_end = seek_drag_end
    page_input.on_submit = go_click

    # ---------- layout: player ----------
    header = ft.Row(
        [
            ft.Container(width=40),
            ft.Column(
                [ft.Text("Now Playing", size=15, weight=ft.FontWeight.W_700, color=W),
                 head_sub],
                spacing=2, horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            ft.Container(content=ring, width=40, alignment=ft.Alignment.CENTER_RIGHT),
        ],
        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
    )

    cover = ft.Row(
        [
            ft.Container(
                content=cover_img, width=COVER, height=COVER, border_radius=30,
                clip_behavior=ft.ClipBehavior.ANTI_ALIAS, bgcolor=ft.Colors.with_opacity(0.2, W),
                border=ft.Border.all(1, ft.Colors.with_opacity(0.18, W)),
                shadow=ft.BoxShadow(blur_radius=40, color=ft.Colors.with_opacity(0.45, "#000000"),
                                    offset=ft.Offset(0, 16)),
            )
        ],
        alignment=ft.MainAxisAlignment.CENTER,
    )

    title_row = ft.Column(
        [
            title_text,
            ft.Row([cover_sub, cover_badge], spacing=10,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            status, prep_err, preview,
        ],
        spacing=4,
    )

    play_circle = ft.Container(
        content=play_btn, width=76, height=76, border_radius=38,
        alignment=ft.Alignment.CENTER, gradient=ACCENT_GRAD,
        shadow=ft.BoxShadow(blur_radius=28, color=ft.Colors.with_opacity(0.55, ACCENT),
                            offset=ft.Offset(0, 10)),
    )

    controls_row = ft.Row(
        [back_btn, prev_btn, play_circle, next_btn, fwd_btn],
        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )

    player_view = ft.Column(
        [
            header,
            cover,
            title_row,
            ft.Column(
                [seek_slider,
                 ft.Row([pos_text, dur_text], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)],
                spacing=0,
            ),
            controls_row,
        ],
        spacing=8, expand=True,
        alignment=ft.MainAxisAlignment.SPACE_EVENLY,
    )

    # ---------- layout: pages ----------
    def make_chip(key, label):
        txt = ft.Text(label, size=13, color=MUTED, weight=ft.FontWeight.W_500)
        cont = ft.Container(
            content=txt,
            padding=ft.Padding(left=16, top=8, right=16, bottom=8),
            border_radius=22, bgcolor=SURFACE, border=ft.Border.all(1, HAIRLINE),
            ink=True, on_click=chip_tap(key),
        )
        chip_refs[key] = (cont, txt)
        return cont

    filter_row = ft.Row(
        [make_chip("all", "All"), make_chip("new", "Not listened"),
         make_chip("done", "Listened"), make_chip("ready", "Audio ready")],
        spacing=8, scroll=ft.ScrollMode.AUTO,
    )
    style_chips()

    pages_header = ft.Column(
        [
            pages_title,
            ft.Row([stat_num, stat_of], spacing=8,
                   vertical_alignment=ft.CrossAxisAlignment.END),
            stat_bar,
            pages_sub,
            ft.Row(
                [
                    page_input,
                    ft.FilledButton(
                        content="Go", on_click=go_click,
                        style=ft.ButtonStyle(bgcolor=ACCENT, color=W),
                    ),
                ],
                spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            filter_row,
            upnext_section,
            ft.Text("All pages", size=17, weight=ft.FontWeight.W_700, color=TEXT),
            hint_text,
            pages_empty,
        ],
        spacing=12,
    )

    pages_view = ft.Column([pages_list], spacing=0, expand=True, visible=False)

    # ---------- layout: library ----------
    library_view = ft.Column(
        [
            ft.Row(
                [
                    ft.Text("Library", size=24, weight=ft.FontWeight.W_800, color=TEXT),
                    ft.FilledButton(
                        content="Add PDF", icon=ft.Icons.ADD_ROUNDED, on_click=pick_pdf,
                        style=ft.ButtonStyle(bgcolor=ACCENT, color=W),
                    ),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            ),
            library_sub,
            library_empty,
            library_list,
        ],
        spacing=10, expand=True, visible=False,
    )

    # ---------- layout: floating pill nav ----------
    def nav_item(name, icon_name, tip):
        on = name == "player"
        icon = ft.Icon(icon_name, size=24, color=W if on else DIM)
        cont = ft.Container(
            content=icon, width=64, height=46, border_radius=23,
            alignment=ft.Alignment.CENTER, gradient=ACCENT_GRAD if on else None,
            ink=True, on_click=nav_tap(name), tooltip=tip,
        )
        nav_refs[name] = (cont, icon)
        return cont

    nav_bar = ft.Row(
        [
            ft.Container(
                content=ft.Row(
                    [
                        nav_item("player", ft.Icons.HEADPHONES_ROUNDED, "Player"),
                        nav_item("pages", ft.Icons.FORMAT_LIST_NUMBERED_ROUNDED, "Pages"),
                        nav_item("library", ft.Icons.LIBRARY_BOOKS_ROUNDED, "Library"),
                    ],
                    spacing=6, tight=True,
                ),
                padding=6, border_radius=32,
                bgcolor=ft.Colors.with_opacity(0.92, "#120E2B"),
                border=ft.Border.all(1, ft.Colors.with_opacity(0.14, W)),
                shadow=ft.BoxShadow(blur_radius=24, color=ft.Colors.with_opacity(0.5, "#000000"),
                                    offset=ft.Offset(0, 8)),
            )
        ],
        alignment=ft.MainAxisAlignment.CENTER,
    )

    bg_box = ft.Container(
        expand=True, gradient=BG_PLAYER,
        content=ft.SafeArea(
            expand=True,
            content=ft.Container(
                padding=ft.Padding(left=20, top=8, right=20, bottom=12),
                expand=True,
                content=ft.Column(
                    [
                        ft.Column([player_view, pages_view, library_view], expand=True),
                        nav_bar,
                    ],
                    expand=True, spacing=12,
                ),
            ),
        ),
    )
    page.add(bg_box)

    # ---------- start-up: reopen the last book, no waiting ----------
    async def startup():
        try:
            books = await asyncio.to_thread(list_books)
        except Exception:
            books = []
        if books:
            await open_book(books[0]["id"], view="player")
        else:
            refresh_library()
            switch_view("library")

    page.run_task(startup)


ft.run(main)