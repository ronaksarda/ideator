"""Records a 2-3 minute walkthrough of every feature, with no captions, for a voiceover. Waits for the agent are
sped up 4x so the graph walk stays readable. Writes demo/walkthrough.mp4 and demo/shotlist.md (timestamps in the
final video). Usage: python scratch/record_walkthrough.py"""
import json
import shutil
import subprocess
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
OUT = Path("demo")
FAST = 4.0
segments, marks = [], []
T0 = time.time()


def now() -> float:
    return time.time() - T0


def mark(text: str) -> None:
    marks.append((now(), text))
    print(f"{now():6.1f}s  {text}", flush=True)


def wait_turn(page) -> None:
    start = now()
    page.wait_for_function("() => !document.getElementById('send').classList.contains('busy')", timeout=600_000, polling=500)
    segments.append((start, now(), FAST))
    page.wait_for_timeout(1200)


def pause(page, s: float) -> None:
    page.wait_for_timeout(int(s * 1000))


def scroll_reply(page, frac: float, ms: int = 2200) -> None:
    page.evaluate("""(f) => { const m = [...document.querySelectorAll('.msg.agent')].pop(); const feed = document.getElementById('feed');
      feed.scrollTo({top: m.offsetTop + (m.offsetHeight - feed.clientHeight) * f, behavior: 'smooth'}); }""", frac)
    page.wait_for_timeout(ms)


def answer_next(page) -> bool:
    chip = page.locator(".chip-next")
    if not chip.count():
        return False
    chip.click()
    pause(page, 1.2)
    page.type("#msg", "Yes, go with your suggestion.", delay=35)
    pause(page, 0.6)
    page.click("#send")
    return True


def out_time(t: float) -> float:
    """Raw recording time to time in the cut video."""
    o = 0.0
    prev = 0.0
    for a, b, sp in segments:
        if t <= a:
            break
        o += a - prev
        o += (min(t, b) - a) / sp
        prev = b
        if t < b:
            return o
    return o + max(0.0, t - prev)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    raw = OUT / "raw-walk"
    shutil.rmtree(raw, ignore_errors=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, record_video_dir=str(raw),
                                  record_video_size={"width": 1440, "height": 900}, color_scheme="light")
        ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin=BASE)
        page = ctx.new_page()
        page.goto(f"{BASE}/?debug=1")
        pause(page, 1.5)
        if page.locator(".msg").count():
            page.click("#new-session")
        pause(page, 1)

        mark("Start screen: what Ideator does, three steps, example ideas. The mascot says hi.")
        pause(page, 5)
        page.locator("#empty .chip", has_text="Cost-capped logging").click()
        mark("Pick an example idea (or type your own) and send it.")
        pause(page, 2)
        page.click("#send")

        mark("Research starts: SerpApi searches Google, Google Play and Reddit. Results are written into FalkorDB.")
        pause(page, 3)
        mark("The agent picks graph tools over MCP one at a time. Each step lights up the nodes and edges it queried; "
             "the step name shows on the map, the Cypher shows in the chat (debug mode).")
        wait_turn(page)

        mark("Turn done. The crowding score, the version list and the full map appear.")
        pause(page, 3)
        page.evaluate("() => { const t = document.querySelector('.msg .trace'); if (t) t.scrollIntoView({behavior: 'smooth'}); }")
        pause(page, 2)
        cyph = page.locator(".msg .trace details summary").first
        if cyph.count():
            cyph.click()
            mark("Every agent step: what it found, the MCP tool, and the real Cypher it ran on this session's graph.")
            pause(page, 5)
            cyph.click()
        mark("The report: the idea, what it means, how to improve it.")
        scroll_reply(page, 0.18, 4000)
        mark("Competitor table with real prices from Google Play, and what users say.")
        scroll_reply(page, 0.4, 4500)
        mark("Pricing, what to build, how to build it.")
        scroll_reply(page, 0.65, 4000)
        mark("Every user quote links to its source.")
        scroll_reply(page, 0.85, 3500)
        mark("The build brief: five decisions. The evidence settles the pain and the price; you make the other three.")
        scroll_reply(page, 1.0, 4500)

        btn = page.locator(".btn-reasoning").last
        if btn.count():
            btn.click()
            mark("Show evidence on map: zooms to the nodes the agent's decision is based on.")
            pause(page, 4)

        page.click("#tab-plan")
        mark("Action plan: the plan in one paragraph, five slots with progress, then a numbered build order.")
        pause(page, 6)
        card = page.locator(".plan-card:not(.brief)").first
        if card.count():
            card.click()
            mark("Click a build step: the map jumps to that feature and explains it, with the companies and complaints behind it.")
            pause(page, 6)
            if page.is_visible("#insp-close"):
                page.click("#insp-close")

        box = page.locator("#split").bounding_box()
        if box:
            y = box["y"] + 400
            page.mouse.move(box["x"] + 3, y); page.mouse.down(); page.mouse.move(box["x"] - 160, y, steps=15); page.mouse.up()
            mark("Drag the divider to give the map more room.")
            pause(page, 2.5)
            page.mouse.move(box["x"] - 157, y); page.mouse.down(); page.mouse.move(box["x"] + 3, y, steps=15); page.mouse.up()
            pause(page, 1)

        page.click("#tab-plan")
        for turn in range(2, 6):
            kicker = page.evaluate("() => (document.querySelector('.brief-kicker') || {}).textContent || ''")
            if "locked" in kicker.lower():
                break
            mark(f"Turn {turn}: 'Decide next' asks one question with a suggestion from the evidence. One click fills it in.")
            if not answer_next(page):
                break
            page.click("#tab-map")
            mark(f"Turn {turn}: the agent checks the answer against the graph, saves a Decision node linked by BECAUSE edges "
                 "to its evidence, and adds a new idea version.")
            wait_turn(page)
            page.click("#tab-plan")
            mark("The plan fills up. Decided slots stay decided.")
            pause(page, 4)

        mark("Plan locked: all five decisions made. The map side warms up, each slot gets its own colour.")
        pause(page, 6)

        page.type("#msg", "Which competitor is weakest on price, and how do I beat them?", delay=30)
        page.click("#send")
        page.click("#tab-map")
        mark("A question instead of a change: the agent gets read-only tools, so it can query the graph but not write to it.")
        wait_turn(page)
        mark("The answer comes straight back, grounded in the graph.")
        scroll_reply(page, 0.0, 3000)
        scroll_reply(page, 0.6, 3500)

        first_version = page.locator("#lineage button").first
        if first_version.count():
            first_version.click()
            mark("Idea versions: every change is a new version linked to the one before it (EVOLVED_FROM). Click one to find it on the map.")
            pause(page, 4)

        page.click("#theme")
        mark("Light and dark themes.")
        pause(page, 4)

        page.click("#download")
        mark("Export: the whole session and the final plan as Markdown. Copy it or download it.")
        pause(page, 3)
        page.click("#export-copy")
        pause(page, 2)
        page.click("#export-close")

        page.set_viewport_size({"width": 820, "height": 900})
        mark("On a narrow screen it switches to one view at a time: chat, map, plan.")
        pause(page, 2.5)
        page.click(".viewbar button[data-view='map']")
        pause(page, 2.5)
        page.click(".viewbar button[data-view='plan']")
        pause(page, 2.5)
        page.click(".viewbar button[data-view='chat']")
        pause(page, 1)
        page.set_viewport_size({"width": 1440, "height": 900})
        pause(page, 1)

        sel = page.locator("#sessions-select option")
        if sel.count() > 2:
            value = sel.nth(2).get_attribute("value")
            page.select_option("#sessions-select", value)
            mark("Past sessions: every idea is saved per owner. Pick one to reopen it with its map and plan.")
            pause(page, 5)

        page.click("#new-session")
        mark("New session: back to a clean start.")
        pause(page, 4)
        video = page.video.path()
        ctx.close()
        browser.close()

    cut(Path(video), OUT / "walkthrough.mp4")
    lines = ["# Walkthrough shot list", "", "Timestamps are in the final video (demo/walkthrough.mp4).", ""]
    for t, text in marks:
        o = out_time(t)
        lines.append(f"- **{int(o // 60)}:{o % 60:04.1f}** {text}")
    (OUT / "shotlist.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUT / "walk-segments.json").write_text(json.dumps(segments))


def cut(src: Path, dst: Path) -> None:
    parts, t = [], 0.0
    for a, b, speed in segments:
        if a > t:
            parts.append((t, a, 1.0))
        parts.append((a, b, speed))
        t = b
    parts.append((t, None, 1.0))
    chains = [f"[0:v]trim=start={a:.2f}{f':end={b:.2f}' if b is not None else ''},setpts=(PTS-STARTPTS)/{sp}[v{i}]"
              for i, (a, b, sp) in enumerate(parts)]
    graph = ";".join(chains) + ";" + "".join(f"[v{i}]" for i in range(len(parts))) + f"concat=n={len(parts)}:v=1:a=0,fps=30[out]"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-filter_complex", graph, "-map", "[out]",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", str(dst)], check=True)
    print("wrote", dst)


if __name__ == "__main__":
    main()
