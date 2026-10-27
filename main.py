import asyncio
import hashlib
import io
import json
import os
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

# ---------------- colours ----------------
W = ft.Colors.WHITE
GRAD_TOP, GRAD_MID, GRAD_BOT = "#8E7CF0", "#7C7EEC", "#6A8AE6"
GLASS = ft.Colors.with_opacity(0.16, W)
GLASS_STRONG = ft.Colors.with_opacity(0.30, W)
GLASS_BORDER = ft.Colors.with_opacity(0.30, W)
SOFT = ft.Colors.with_opacity(0.72, W)
FAINT = ft.Colors.with_opacity(0.50, W)
PINK = "#FFC4E6"
PLAY_BG = "#F4F0FF"
PLAY_FG = "#6A5BD8"
OK_GREEN = "#A6F2CB"
WAIT_YELLOW = "#FFE29A"
ERR_RED = "#FFA3AE"


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


def err_text(err, n=110):
    """Always says what kind of error it was, even when the message is empty
    (a timeout has an empty message, which is why errors looked 'unknown')."""
    msg = str(err).strip()
    name = type(err).__name__
    return f"{name}: {msg}"[:n] if msg else name


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
    page.bgcolor = GRAD_MID
    page.padding = 0

    state = {
        "book_id": None,
        "meta": None,
        "pages": [],
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
    }
    rows = []          # (container, icon, subtitle, snippet) per page in "All pages"
    row_last = []      # last drawn state per row, to skip needless updates

    # ---------- controls: player view ----------
    head_sub = ft.Text("No PDF loaded", size=12, color=SOFT, text_align=ft.TextAlign.CENTER)
    ring = ft.ProgressRing(visible=False, width=18, height=18, stroke_width=2, color=W)

    cover_num = ft.Text("-", size=64, weight=ft.FontWeight.W_800, color=W)
    cover_of = ft.Text("Open a PDF to begin", size=14, color=SOFT)
    preview = ft.Text("", size=14, color=ft.Colors.with_opacity(0.9, W), selectable=True,
                      text_align=ft.TextAlign.CENTER)

    title_text = ft.Text("Your audiobook", size=24, weight=ft.FontWeight.W_700, color=W,
                         max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
    status = ft.Text("Add a PDF from the Library", size=14, color=SOFT,
                     max_lines=3, overflow=ft.TextOverflow.ELLIPSIS)
    prep_err = ft.Text("", size=11, color=ERR_RED, visible=False, max_lines=3,
                       overflow=ft.TextOverflow.ELLIPSIS)

    seek_slider = ft.Slider(
        min=0, max=1, value=0, disabled=True,
        active_color=PINK, inactive_color=ft.Colors.with_opacity(0.3, W), thumb_color=W,
    )
    pos_text = ft.Text("0:00", size=12, color=SOFT)
    dur_text = ft.Text("0:00", size=12, color=SOFT)

    play_btn = ft.FilledIconButton(
        icon=ft.Icons.PLAY_ARROW_ROUNDED, icon_size=40, icon_color=PLAY_FG, bgcolor=PLAY_BG,
        style=ft.ButtonStyle(shape=ft.CircleBorder(), padding=ft.Padding.all(20)),
    )
    prev_btn = ft.IconButton(icon=ft.Icons.SKIP_PREVIOUS_ROUNDED, icon_size=36, icon_color=W,
                             tooltip="Previous page")
    next_btn = ft.IconButton(icon=ft.Icons.SKIP_NEXT_ROUNDED, icon_size=36, icon_color=W,
                             tooltip="Next page")
    back_btn = ft.IconButton(icon=ft.Icons.REPLAY_10_ROUNDED, icon_size=28, icon_color=SOFT,
                             tooltip=f"Back {SKIP_SECONDS} seconds")
    fwd_btn = ft.IconButton(icon=ft.Icons.FORWARD_10_ROUNDED, icon_size=28, icon_color=SOFT,
                            tooltip=f"Forward {SKIP_SECONDS} seconds")

    # ---------- controls: pages view ----------
    pages_title = ft.Text("All pages", size=22, weight=ft.FontWeight.W_700, color=W,
                          max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
    pages_sub = ft.Text("No book open", size=13, color=SOFT)
    page_input = ft.TextField(
        label="Page", width=90, dense=True, text_align=ft.TextAlign.CENTER,
        keyboard_type=ft.KeyboardType.NUMBER, color=W, border_color=GLASS_BORDER,
    )
    ready_switch = ft.Switch(label="Ready only", value=False, active_color=PINK)
    pages_list = ft.ListView(controls=[], spacing=8, expand=True)
    pages_empty = ft.Text("No book open. Choose one from the Library.", size=14, color=SOFT)

    # ---------- controls: library view ----------
    library_sub = ft.Text("", size=13, color=SOFT)
    library_list = ft.ListView(controls=[], spacing=10, expand=True)
    library_empty = ft.Text("Nothing saved yet. Tap Add PDF to start your library.",
                            size=14, color=SOFT)

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
        if i in state["cache"]:
            return "ready"
        if i in state["inflight"]:
            return "loading"
        if i in state["failed"]:
            return "failed"
        return "wait"

    ROW_ICON = {
        "current": (ft.Icons.GRAPHIC_EQ_ROUNDED, W),
        "ready": (ft.Icons.CHECK_CIRCLE_ROUNDED, OK_GREEN),
        "loading": (ft.Icons.HOURGLASS_TOP_ROUNDED, WAIT_YELLOW),
        "failed": (ft.Icons.ERROR_OUTLINE_ROUNDED, ERR_RED),
        "wait": (ft.Icons.SCHEDULE_ROUNDED, FAINT),
    }

    def refresh_rows():
        only_ready = bool(ready_switch.value)
        for i, (cont, icon, sub, snippet) in enumerate(rows):
            s = row_status(i)
            err = state["errors"].get(i, "") if s == "failed" else ""
            key = (s, only_ready, err)
            if row_last[i] == key:
                continue
            row_last[i] = key
            icon.icon, icon.color = ROW_ICON[s]
            cont.bgcolor = GLASS_STRONG if s == "current" else GLASS
            cont.visible = (not only_ready) or s in ("ready", "current")
            if err:
                sub.value, sub.color = f"Failed: {err} (tap to retry)", ERR_RED
            else:
                sub.value, sub.color = snippet, SOFT

    def build_rows():
        rows.clear()
        row_last.clear()
        controls = []
        for i, text in enumerate(state["pages"]):
            icon = ft.Icon(ft.Icons.SCHEDULE_ROUNDED, size=22, color=FAINT)
            snippet = short(text, 70)
            sub = ft.Text(snippet, size=12, color=SOFT, max_lines=2,
                          overflow=ft.TextOverflow.ELLIPSIS)
            cont = ft.Container(
                content=ft.Row(
                    [
                        ft.Column(
                            [
                                ft.Text(f"Page {i + 1}", size=15, weight=ft.FontWeight.W_600, color=W),
                                sub,
                            ],
                            spacing=2, expand=True,
                        ),
                        icon,
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=12,
                ),
                padding=ft.Padding(left=16, top=12, right=16, bottom=12),
                border_radius=18, bgcolor=GLASS,
                border=ft.Border.all(1, GLASS_BORDER),
                ink=True, on_click=row_tap(i),
            )
            rows.append((cont, icon, sub, snippet))
            row_last.append(None)
            controls.append(cont)
        pages_list.controls = controls
        pages_empty.visible = not controls
        refresh_rows()

    def refresh_ui():
        pages, i = state["pages"], state["index"]
        n = len(pages)
        if n:
            title_text.value = state["name"]
            pages_title.value = state["name"]
            cover_num.value = str(i + 1)
            cover_of.value = f"of {n} pages"
            preview.value = short(pages[i], 260)
            page_input.value = str(i + 1)
            page_input.error_text = None
            refresh_rows()
        page.update()

    def refresh_prep():
        n = len(state["pages"])
        if not n:
            return
        text = f"Audio ready for {len(state['cache'])} of {n} pages"
        if state["failed"]:
            text += f" - {len(state['failed'])} failed"
        head_sub.value = text
        pages_sub.value = text
        if state["failed"]:
            idx = max(state["failed"], key=state["failed"].get)
            prep_err.value = f"Page {idx + 1}: {state['errors'].get(idx, 'unknown problem')}"
            prep_err.visible = True
        else:
            prep_err.visible = False
        refresh_rows()
        page.update()

    def switch_view(name):
        state["view"] = name
        player_view.visible = name == "player"
        pages_view.visible = name == "pages"
        library_view.visible = name == "library"
        nav_player.icon_color = W if name == "player" else FAINT
        nav_pages.icon_color = W if name == "pages" else FAINT
        nav_library.icon_color = W if name == "library" else FAINT
        page.update()

    # ---------- library list ----------
    def refresh_library():
        books = list_books()
        controls = []
        for meta in books:
            current = meta["id"] == state["book_id"]
            mb = meta.get("audio_bytes", 0) / (1024 * 1024)
            sub = (f"{meta['n_pages']} pages  |  page {meta.get('last_page', 0) + 1}  |  "
                   f"audio {meta['audio_ready']}/{meta['n_pages']}  |  {mb:.0f} MB")
            controls.append(ft.Container(
                content=ft.Row(
                    [
                        ft.Icon(ft.Icons.PICTURE_AS_PDF_ROUNDED, size=28,
                                color=PINK if current else SOFT),
                        ft.Column(
                            [
                                ft.Text(meta["name"], size=15, weight=ft.FontWeight.W_600, color=W,
                                        max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                                ft.Text(sub, size=12, color=SOFT, max_lines=2,
                                        overflow=ft.TextOverflow.ELLIPSIS),
                            ],
                            spacing=2, expand=True,
                        ),
                        ft.IconButton(icon=ft.Icons.DELETE_OUTLINE_ROUNDED, icon_color=FAINT,
                                      tooltip="Delete this book", on_click=delete_tap(meta)),
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=12,
                ),
                padding=ft.Padding(left=16, top=10, right=6, bottom=10),
                border_radius=20, bgcolor=GLASS_STRONG if current else GLASS,
                border=ft.Border.all(1, GLASS_BORDER),
                ink=True, on_click=book_tap(meta["id"]),
            ))
        library_list.controls = controls
        library_empty.visible = not controls
        total = sum(m.get("audio_bytes", 0) for m in books) / (1024 * 1024)
        library_sub.value = (f"{len(books)} saved  |  {total:.0f} MB of audio on this device"
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
        try:
            save_meta(meta)
        except Exception:
            pass

    # ---------- opening / closing books ----------
    def clear_book_ui():
        state.update(book_id=None, meta=None, pages=[], index=0, name="")
        build_rows()
        title_text.value = "Your audiobook"
        pages_title.value = "All pages"
        pages_sub.value = "No book open"
        cover_num.value = "-"
        cover_of.value = "Open a PDF to begin"
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
            state["book_id"] = book_id
            state["meta"] = meta
            state["pages"] = pages
            state["name"] = meta["name"]
            state["index"] = max(0, min(int(meta.get("last_page", 0)), len(pages) - 1))
            state["cache"] = await asyncio.to_thread(scan_audio, book_id)
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
                content=ft.Text(f"{meta['name']}\n\nThe saved text and audio will be removed "
                                f"from this device."),
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
        set_play_icon(False)

    async def advance(my_token):
        if my_token != state["token"]:      # already moved on
            return
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
            src=path, autoplay=True,
            on_state_change=on_done, on_position_change=on_pos,
        )
        state["audio"] = audio
        page.services.append(audio)
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

    async def seek_to(seconds):
        a = state["audio"]
        if a is None or not state["dur"]:
            return
        seconds = max(0.0, seconds)
        if seconds >= state["dur"] - 0.5:       # skipped past the end
            await advance(state["token"])
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

    def ready_toggled(e):
        for i in range(len(row_last)):
            row_last[i] = None
        refresh_rows()
        page.update()

    async def show_player(e):
        switch_view("player")

    async def show_pages(e):
        refresh_rows()
        switch_view("pages")

    async def show_library(e):
        refresh_library()
        switch_view("library")

    play_btn.on_click = play_click
    prev_btn.on_click = prev_click
    next_btn.on_click = next_click
    back_btn.on_click = back_click
    fwd_btn.on_click = fwd_click
    seek_slider.on_change_start = seek_drag_start
    seek_slider.on_change = seek_dragging
    seek_slider.on_change_end = seek_drag_end
    page_input.on_submit = go_click
    ready_switch.on_change = ready_toggled

    # ---------- layout ----------
    def glass(content, pad=18, radius=28):
        return ft.Container(
            content=content, padding=pad, border_radius=radius, bgcolor=GLASS,
            border=ft.Border.all(1, GLASS_BORDER),
        )

    header = ft.Row(
        [
            ft.Container(width=40),
            ft.Column(
                [ft.Text("Now Playing", size=16, weight=ft.FontWeight.W_600, color=W), head_sub],
                spacing=2, horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            ft.Container(content=ring, width=40, alignment=ft.Alignment.CENTER),
        ],
        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
    )

    cover = ft.Container(
        content=ft.Column(
            [
                ft.Icon(ft.Icons.HEADPHONES_ROUNDED, color=FAINT, size=30),
                ft.Text("Page", size=14, color=SOFT),
                cover_num,
                cover_of,
                ft.Container(height=6),
                ft.Column([preview], scroll=ft.ScrollMode.AUTO, height=70,
                          horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=0,
        ),
        height=330, padding=20, border_radius=32,
        gradient=ft.LinearGradient(
            begin=ft.Alignment.TOP_LEFT, end=ft.Alignment.BOTTOM_RIGHT,
            colors=["#E8A7E0", "#A98BEF", "#7F97EE"],
        ),
        border=ft.Border.all(1, GLASS_BORDER),
    )

    title_row = ft.Column([title_text, status, prep_err], spacing=3)

    controls_row = ft.Row(
        [back_btn, prev_btn, play_btn, next_btn, fwd_btn],
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
        spacing=16, expand=True, scroll=ft.ScrollMode.AUTO,
    )

    pages_view = ft.Column(
        [
            pages_title,
            pages_sub,
            ft.Row(
                [page_input, ft.FilledButton(content="Go", on_click=go_click), ready_switch],
                spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            pages_empty,
            pages_list,
        ],
        spacing=10, expand=True, visible=False,
    )

    library_view = ft.Column(
        [
            ft.Row(
                [
                    ft.Text("Library", size=22, weight=ft.FontWeight.W_700, color=W),
                    ft.FilledButton(
                        content="Add PDF", icon=ft.Icons.ADD_ROUNDED, on_click=pick_pdf,
                        style=ft.ButtonStyle(bgcolor=PLAY_BG, color=PLAY_FG),
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

    nav_player = ft.IconButton(icon=ft.Icons.HEADPHONES_ROUNDED, icon_size=26, icon_color=W,
                               tooltip="Player", on_click=show_player)
    nav_pages = ft.IconButton(icon=ft.Icons.FORMAT_LIST_NUMBERED_ROUNDED, icon_size=26,
                              icon_color=FAINT, tooltip="All pages", on_click=show_pages)
    nav_library = ft.IconButton(icon=ft.Icons.LIBRARY_BOOKS_ROUNDED, icon_size=26,
                                icon_color=FAINT, tooltip="Library", on_click=show_library)

    nav_bar = glass(
        ft.Row([nav_player, nav_pages, nav_library], alignment=ft.MainAxisAlignment.SPACE_AROUND),
        pad=ft.Padding(left=8, top=4, right=8, bottom=4), radius=26,
    )

    page.add(
        ft.Container(
            expand=True,
            gradient=ft.LinearGradient(
                begin=ft.Alignment.TOP_CENTER, end=ft.Alignment.BOTTOM_CENTER,
                colors=[GRAD_TOP, GRAD_MID, GRAD_BOT],
            ),
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
    )

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