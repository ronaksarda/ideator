"""Records the demo video against the running app (http://127.0.0.1:8000): three turns of one idea, from first
search to a finished build brief, then the Temporal UI. Waits are sped up afterwards with ffmpeg.
Usage: python scratch/record_demo.py  ->  demo/ideator-demo.mp4"""
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
OUT = Path("demo")
IDEA = sys.argv[1] if len(sys.argv) > 1 else "Budget meal planner for college students who share a kitchen"
segments = []  # (start_s, end_s, speed): stretches to fast-forward in the final cut
T0 = time.time()

CAPTION_JS = """(t) => {
  let el = document.getElementById('demo-cap');
  if (!el) { el = document.createElement('div'); el.id = 'demo-cap';
    el.style.cssText = 'position:fixed;left:50%;bottom:22px;transform:translateX(-50%);z-index:99999;max-width:80%;'
      + 'background:rgba(10,10,20,.88);color:#fff;font:600 20px/1.4 system-ui,sans-serif;padding:12px 22px;border-radius:12px;'
      + 'box-shadow:0 6px 30px rgba(0,0,0,.4);text-align:center;pointer-events:none';
    document.body.appendChild(el); }
  el.style.display = t ? 'block' : 'none'; el.textContent = t;
}"""


def now() -> float:
    return time.time() - T0


def cap(page, text: str, hold: float = 0) -> None:
    page.evaluate(CAPTION_JS, text)
    if hold:
        page.wait_for_timeout(int(hold * 1000))


def wait_turn(page, label: str) -> None:
    """Wait for the agent to finish a turn; that stretch plays fast in the final video."""
    start = now()
    page.wait_for_function("() => !document.getElementById('send').disabled", timeout=600_000, polling=1000)
    segments.append((start, now(), 40.0))
    print(f"{label}: {now() - start:.0f}s", flush=True)
    page.wait_for_timeout(1500)


def send(page, text: str) -> None:
    page.fill("#msg", text)
    page.wait_for_timeout(500)
    page.click("#send")
    page.wait_for_timeout(1500)


def scroll_feed(page, to: str = "bottom", ms: int = 2500) -> None:
    page.evaluate(f"() => {{ const f = document.getElementById('feed'); f.scrollTo({{top: {'f.scrollHeight' if to == 'bottom' else '0'}, behavior: 'smooth'}}); }}")
    page.wait_for_timeout(ms)


def show_last_reply(page, captions) -> None:
    """Scroll through the latest reply section by section, one caption per stop."""
    for frac, text in captions:
        page.evaluate("""(f) => { const m = [...document.querySelectorAll('.msg')].pop(); const feed = document.getElementById('feed');
          feed.scrollTo({top: m.offsetTop + (m.offsetHeight - feed.clientHeight) * f, behavior: 'smooth'}); }""", frac)
        cap(page, text, 3)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    raw = OUT / "raw"
    shutil.rmtree(raw, ignore_errors=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, record_video_dir=str(raw),
                                  record_video_size={"width": 1440, "height": 900}, accept_downloads=True)
        page = ctx.new_page()
        page.goto(f"{BASE}/?debug=1")
        page.wait_for_timeout(1500)
        if page.is_visible("#new-session"):
            page.click("#new-session")
        cap(page, "Ideator: check a startup idea against the real market, and leave with a plan you can build.", 3)

        # ---- turn 1 ----
        send(page, IDEA)
        cap(page, "Turn 1 (sped up): SerpApi searches Google, Google Play and Reddit. Results go into FalkorDB. "
                  "Gemma then picks graph tools over MCP, step by step.", 0)
        wait_turn(page, "turn 1")
        page.evaluate("() => { const t = document.querySelector('.msg .trace'); if (t) t.scrollIntoView({behavior: 'smooth'}); }")
        cap(page, "Each step ran real Cypher on this session's graph. TabPFN priced it from stored competitors.", 3.5)
        show_last_reply(page, [(0.3, "Report: real competitors and prices. Every user quote links to its source."),
                               (1.0, "It ends with a build brief: 5 decisions. Evidence fills 2; you make the rest.")])
        page.click("#tab-map")
        cap(page, "Market map, read back from FalkorDB: idea, companies with price, features, what users say.", 3.5)
        page.click("#tab-plan")
        cap(page, "Action plan: your plan in one paragraph, the brief, then the build order.", 4)

        # ---- turns 2+: answer the one open question until the brief is complete ----
        for turn in (2, 3, 4, 5):
            done = page.evaluate("() => [...document.querySelectorAll('.plan-card.brief h3')].map(h => h.textContent).join(' ')")
            if "5/5" in done or not page.locator(".chip-next").count():
                break
            cap(page, f"Turn {turn}: one click answers the open question.", 1.5)
            page.locator(".chip-next").click()
            page.fill("#msg", page.input_value("#msg") + "Yes, go with your suggestion.")
            page.wait_for_timeout(1000)
            page.click("#send")
            cap(page, f"Turn {turn} (sped up): the agent checks the answer against the graph and records a decision.", 0)
            wait_turn(page, f"turn {turn}")
            page.click("#tab-plan")
            cap(page, "The plan fills up. Decided slots stay decided.", 2.5)
        cap(page, "Plan locked: who pays, the pain, the edge, the price and version 1, plus a build order.", 4.5)

        # ---- Temporal ----
        try:
            page.goto("http://localhost:8233/namespaces/default/workflows", timeout=15_000)
            page.wait_for_timeout(1500)
            cap(page, "Temporal: search and report run as retried, durable workflows.", 3)
        except Exception as e:
            print("Temporal UI not reachable:", e)
        page.goto(f"{BASE}/")
        page.wait_for_timeout(800)
        cap(page, "Ideator. FalkorDB + Gemma + MCP + TabPFN. Graph Hacks Track 01: Agents That Act on Connected Data.", 3)
        video = page.video.path()
        ctx.close()
        browser.close()

    (OUT / "segments.json").write_text(json.dumps(segments))
    cut(Path(video), OUT / "ideator-demo.mp4")


def cut(src: Path, dst: Path) -> None:
    """Concatenate normal-speed and fast-forwarded stretches into one mp4."""
    parts, t = [], 0.0
    for a, b, speed in segments:
        if a > t:
            parts.append((t, a, 1.0))
        parts.append((a, b, speed))
        t = b
    parts.append((t, None, 1.0))
    chains = []
    for i, (a, b, speed) in enumerate(parts):
        end = f":end={b:.2f}" if b is not None else ""
        chains.append(f"[0:v]trim=start={a:.2f}{end},setpts=(PTS-STARTPTS)/{speed}[v{i}]")
    graph = ";".join(chains) + ";" + "".join(f"[v{i}]" for i in range(len(parts))) + f"concat=n={len(parts)}:v=1:a=0[out]"
    subprocess.run(["ffmpeg", "-y", "-i", str(src), "-filter_complex", graph, "-map", "[out]",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", str(dst)], check=True)
    print("wrote", dst)


if __name__ == "__main__":
    main()
