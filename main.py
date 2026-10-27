import os
import io
import tempfile
import asyncio
import uuid

import flet as ft
import flet_audio as fta
import PyPDF2
from gtts import gTTS

import re
import edge_tts


VOICE = "en-IN-NeerjaNeural"   
RATE = "-3%"      


def extract_pages(source):
    """source can be a file path (str) or raw bytes."""
    pages = []
    if isinstance(source, (bytes, bytearray)):
        reader = PyPDF2.PdfReader(io.BytesIO(source))
        for p in reader.pages:
            t = p.extract_text()
            if t and t.strip():
                pages.append(t.replace("\n", " "))
    else:
        with open(source, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            for p in reader.pages:
                t = p.extract_text()
                if t and t.strip():
                    pages.append(t.replace("\n", " "))
    return pages



def clean_text(t: str) -> str:
    t = re.sub(r"(\w)-\s+(\w)", r"\1\2", t)   # fix words split across lines: "exam- ple"
    t = re.sub(r"\s+", " ", t)                # collapse extra spaces
    return t.strip()

async def make_mp3(text, out_path):
    comm = edge_tts.Communicate(clean_text(text), VOICE, rate=RATE)
    await comm.save(out_path)


def main(page: ft.Page):
    page.title = "PDF Audiobook Player"
    page.vertical_alignment = ft.MainAxisAlignment.CENTER

    state = {
        "pages": [],
        "index": 0,
        "audio": None,       # current Audio control
        "playing": False,
        "name": "",
        "token": 0,          # invalidates stale audio loads
        "session": 0,        # new PDF = new session
        "cache": {},         # page index -> mp3 path
        "inflight": {},      # page index -> running task
        "failed": set(),
        "bg": [],           
    }

    status = ft.Text("No PDF loaded.", size=18, weight=ft.FontWeight.BOLD,
                     text_align=ft.TextAlign.CENTER)
    ring = ft.ProgressRing(visible=False, width=20, height=20)
    play_btn = ft.IconButton(icon=ft.Icons.PLAY_CIRCLE, icon_size=60)

    file_picker = ft.FilePicker()
    page.services.append(file_picker)

    # ---------- helpers ----------
    def set_busy(busy: bool):
        ring.visible = busy
        play_btn.disabled = busy
        page.update()

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
        play_btn.icon = ft.Icons.PLAY_CIRCLE

    async def on_audio_state(e):
        # auto-advance when the page finishes
        if str(e.state).lower().endswith("completed"):
            state["playing"] = False
            if state["index"] < len(state["pages"]) - 1:
                state["index"] += 1
                await play_current()
            else:
                play_btn.icon = ft.Icons.PLAY_CIRCLE
                status.value = "Finished."
                page.update()
                
    
    async def _generate(idx, session):
        path = os.path.join(tempfile.gettempdir(), f"page_{uuid.uuid4().hex}.mp3")
        try:
            await make_mp3(state["pages"][idx], path)
            if session == state["session"]:
                state["cache"][idx] = path
            return path
        finally:
            if session == state["session"]:
                state["inflight"].pop(idx, None)

    async def get_audio(idx):
        cached = state["cache"].get(idx)
        if cached and os.path.exists(cached):
            return cached
        task = state["inflight"].get(idx)
        if task is None:
            task = asyncio.create_task(_generate(idx, state["session"]))
            state["inflight"][idx] = task
        return await asyncio.shield(task)

    async def prefetch_worker(session):
        while session == state["session"]:
            n = len(state["pages"])
            order = list(range(state["index"], n)) + list(range(0, state["index"]))
            idx = next(
                (i for i in order
                 if i not in state["cache"]
                 and i not in state["inflight"]
                 and i not in state["failed"]),
                None,
            )
            if idx is None:
                break  # everything is done
            try:
                await get_audio(idx)
            except Exception:
                state["failed"].add(idx)
                await asyncio.sleep(1)
            # update progress text
            if session == state["session"] and not state["playing"] and not ring.visible:
                prep.value = f"Audio ready: {len(state['cache'])}/{n} pages"
                page.update()
        if session == state["session"]:
            prep.value = f"Audio ready: {len(state['cache'])}/{len(state['pages'])} pages"
            page.update()

    def start_prefetch():
        for t in state["bg"]:
            t.cancel()
        state["bg"] = [
            asyncio.create_task(prefetch_worker(state["session"])) for _ in range(2)
        ]  # 2 pages generated at a time

    def reset_cache():
        state["session"] += 1
        for t in state["bg"]:
            t.cancel()
        for t in state["inflight"].values():
            t.cancel()
        for p in state["cache"].values():
            try:
                os.remove(p)
            except OSError:
                pass
        state["cache"].clear()
        state["inflight"].clear()
        state["failed"].clear()
        state["bg"] = []
    async def play_current():
        if not state["pages"]:
            return
        await stop_audio()
        state["token"] += 1
        my_token = state["token"]
        idx = state["index"]

        if idx not in state["cache"]:
            status.value = f"Preparing page {idx + 1}..."
            set_busy(True)

        try:
            path = await get_audio(idx)
        except Exception as err:
            status.value = f"Audio error (internet needed): {err}"
            set_busy(False)
            return

        if my_token != state["token"]:   # user skipped meanwhile
            return

        audio = fta.Audio(src=path, autoplay=True, on_state_change=on_audio_state)
        state["audio"] = audio
        page.services.append(audio)
        state["playing"] = True
        play_btn.icon = ft.Icons.PAUSE_CIRCLE
        status.value = f"Playing: {state['name']} (Page {idx + 1} of {len(state['pages'])})"
        set_busy(False)

    # ---------- handlers ----------
    async def pick_pdf(e):
        files = await file_picker.pick_files(
            allowed_extensions=["pdf"], with_data=True
        )
        if not files:
            return
        f = files[0]
        await stop_audio()
        reset_cache()  
        status.value = "Extracting text from PDF..."
        set_busy(True)
        try:
            source = f.path if f.path else f.bytes
            pages = await asyncio.to_thread(extract_pages, source)
            if not pages:
                status.value = "No readable text found (scanned PDF?)."
            else:
                state["pages"] = pages
                state["index"] = 0
                state["name"] = f.name
                status.value = f"Ready: {f.name} (Page 1 of {len(pages)})"
                start_prefetch()
        except Exception as err:
            status.value = f"Could not read PDF: {err}"
        set_busy(False)

    async def play_click(e):
        if not state["pages"]:
            return
        a = state["audio"]
        if a is not None and state["playing"]:
            await a.pause()
            state["playing"] = False
            play_btn.icon = ft.Icons.PLAY_CIRCLE
            status.value = f"Paused: Page {state['index'] + 1}"
            page.update()
        elif a is not None:
            await a.resume()
            state["playing"] = True
            play_btn.icon = ft.Icons.PAUSE_CIRCLE
            status.value = f"Playing: Page {state['index'] + 1}"
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

    play_btn.on_click = play_click
    prep = ft.Text("", size=12, text_align=ft.TextAlign.CENTER)
    page.add(
        ft.Row([status, ring], alignment=ft.MainAxisAlignment.CENTER),
        ft.Row([prep], alignment=ft.MainAxisAlignment.CENTER),
        ft.Row(
            [ft.Button(content="Select PDF File", icon=ft.Icons.FOLDER_OPEN,
                       on_click=pick_pdf)],
            alignment=ft.MainAxisAlignment.CENTER,
        ),
        ft.Row(
            [
                ft.IconButton(icon=ft.Icons.SKIP_PREVIOUS, icon_size=40, on_click=prev_click),
                play_btn,
                ft.IconButton(icon=ft.Icons.SKIP_NEXT, icon_size=40, on_click=next_click),
            ],
            alignment=ft.MainAxisAlignment.CENTER,
        ),
    )


ft.run(main)