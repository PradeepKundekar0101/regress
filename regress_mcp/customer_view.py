"""Customer view: film the real bot UI answering the fraud question, and read back what it showed.

A deterministic Playwright script, not an LLM browser: open the bot with ?probe=1 (so the statistics
ignore the request), ask one question, wait for the reply slip, keep it on screen for the viewer, then
close the context so Playwright finalises the video. What the page showed (the specialist banner, the
cited sources, the footer) is parsed from the slip's HTML and returned as facts, so a claim like "the
banner is back" is checked, not just filmed.
"""

import concurrent.futures
import re
import shutil
import subprocess
import tempfile
from html.parser import HTMLParser
from pathlib import Path

from target import config

# The demo's fraud question: the bot UI's "Cards" suggestion, a paraphrase of golden g18.
QUESTION = "There is a charge on my card I did not make."
VIEWPORT = {"width": 900, "height": 720}
REPLY_TIMEOUT_MS = 45_000
PAGE_TIMEOUT_MS = 15_000
LINGER_MS = 2_000
TRANSCODE_TIMEOUT_S = 60
PHASES = ("before", "after")
MEDIA_ROOT = Path(__file__).resolve().parent.parent / ".regress" / "media"
FOOTER = re.compile(r"Prompt v(?P<version>\d+)\s*·\s*(?P<model>[^·]+?)\s*·\s*(?P<ms>[\d,]+)\s*ms")


class _SlipParser(HTMLParser):
    """Collects the banner, the cited source ids and the footer text from one `.slip` element."""

    def __init__(self):
        super().__init__()
        self.banner, self.citations, self.footer = False, [], ""
        self._stack: list[set[str]] = []

    def handle_starttag(self, tag, attrs):
        classes = set((dict(attrs).get("class") or "").split())
        self._stack.append(classes)
        if "handoff" in classes:
            self.banner = True

    def handle_endtag(self, tag):
        if self._stack:
            self._stack.pop()

    def handle_data(self, data):
        inside = set().union(*self._stack) if self._stack else set()
        if "id" in inside and "ledger" in inside and data.strip():
            self.citations.append(data.strip())
        if "meta" in inside:
            self.footer += data


def observe(slip_html: str) -> dict:
    """What a customer saw in one reply slip: banner shown, cited ids, footer prompt version and model."""
    parser = _SlipParser()
    parser.feed(slip_html)
    footer = " ".join(parser.footer.split())
    match = FOOTER.search(footer)
    return {
        "banner": parser.banner,
        "citations": parser.citations,
        "footer": footer,
        "prompt_version": int(match["version"]) if match else None,
        "model": match["model"].strip() if match else None,
        "latency_ms": int(match["ms"].replace(",", "")) if match else None,
    }


def media_dir(incident_id: str) -> Path:
    if not re.fullmatch(r"inc_[0-9]{8}_[0-9]{6}_[0-9a-f]{4}", incident_id):
        raise ValueError(f"not an incident id: {incident_id!r}")
    return MEDIA_ROOT / incident_id


def media_files(incident_id: str, phase: str) -> dict[str, Path]:
    """The saved video and screenshot for a phase, whichever exist. MP4 wins over WebM."""
    if phase not in PHASES:
        raise ValueError(f"phase must be one of {PHASES}")
    folder, found = media_dir(incident_id), {}
    for ext in ("mp4", "webm"):
        if (folder / f"{phase}.{ext}").is_file():
            found["video"] = folder / f"{phase}.{ext}"
            break
    if (folder / f"{phase}.png").is_file():
        found["screenshot"] = folder / f"{phase}.png"
    return found


def _ffmpeg() -> str | None:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


def _transcode(webm: Path) -> Path:
    """MP4 (H.264) plays in Safari; WebM does not reliably. Keep the WebM when there is no ffmpeg."""
    exe = _ffmpeg()
    if exe is None:
        return webm
    mp4 = webm.with_suffix(".mp4")
    done = subprocess.run([exe, "-y", "-loglevel", "error", "-i", str(webm), "-c:v", "libx264",
                           "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(mp4)],
                          capture_output=True, timeout=TRANSCODE_TIMEOUT_S)
    if done.returncode != 0 or not mp4.is_file():
        return webm
    webm.unlink()
    return mp4


def _record(url: str, question: str, folder: Path, phase: str) -> dict:
    from playwright.sync_api import sync_playwright

    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            context = browser.new_context(viewport=VIEWPORT, record_video_dir=tmp, record_video_size=VIEWPORT)
            page = context.new_page()
            page.set_default_timeout(PAGE_TIMEOUT_MS)
            page.goto(url, wait_until="networkidle")
            page.fill("#q", question)
            page.press("#q", "Enter")
            page.wait_for_selector(".slip, .error", timeout=REPLY_TIMEOUT_MS)
            if page.query_selector(".error"):
                raise RuntimeError(f"the bot showed an error: {page.inner_text('.error')}")
            slip = page.query_selector(".slip")
            # Frame the whole exchange: the question at the top, the reply slip below it.
            page.eval_on_selector_all(".ask", "els => els.at(-1).scrollIntoView({block: 'start'})")
            page.wait_for_timeout(LINGER_MS)
            facts = observe(slip.evaluate("el => el.outerHTML"))
            screenshot = folder / f"{phase}.png"
            slip.screenshot(path=str(screenshot))
            video = page.video
            context.close()  # Playwright finalises the video on close.
            webm = folder / f"{phase}.webm"
            for stale in (folder / f"{phase}.mp4", webm):
                stale.unlink(missing_ok=True)
            shutil.move(video.path(), webm)
        finally:
            browser.close()
    return {**facts, "video": _transcode(webm), "screenshot": screenshot}


def capture(incident_id: str, phase: str, question: str = QUESTION) -> dict:
    """Film one question against the live bot. Raises on any failure; callers decide what that means.

    Runs in a worker thread: the Playwright sync API refuses to run inside an asyncio event loop, and the
    MCP server calls tools from one.
    """
    if phase not in PHASES:
        raise ValueError(f"phase must be one of {PHASES}")
    url = f"{config.env('BOT_URL', 'http://localhost:8000').rstrip('/')}/?probe=1"
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(_record, url, question, media_dir(incident_id), phase).result()
    return {**result, "question": question, "url": url}
