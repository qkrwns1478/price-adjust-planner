"""검증 결과 화면 캡처: 실제 LLM(OpenAI)과 외부 API를 호출한다.

    python backend/capture_screens.py

확인용 서버 두 개(키 정상, 외부 API 키 비움)를 따로 띄우고, 설치된 Chrome을 headless로 조작해
docs/images/ 에 PNG로 저장한다. 실행 로그와 계획은 data/screens/ 의 별도 DB에 쓴다 (형상관리 제외).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests
import websockets

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
IMAGES = PROJECT / "docs" / "images"
DATA = PROJECT / "data" / "screens"
CHROME = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
]
PORT, NOKEY_PORT, DEBUG_PORT = 8765, 8766, 9333
WIDTH, HEIGHT = 1440, 1000

sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass
from check_agent import REPLAN, REPRESENTATIVE  # noqa: E402


def server(port: int, db: Path, env: dict[str, str] | None = None) -> subprocess.Popen:
    db.unlink(missing_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)],
        cwd=ROOT,
        env=os.environ | {"PLANNER_DB_PATH": str(db)} | (env or {}),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(60):
        try:
            if requests.get(f"http://127.0.0.1:{port}/api/status", timeout=1).ok:
                return proc
        except requests.RequestException:
            time.sleep(0.5)
    proc.kill()
    raise RuntimeError(f"{port} 서버가 뜨지 않았습니다.")


class Page:
    def __init__(self, ws):
        self.ws, self.n = ws, 0

    async def send(self, method: str, **params):
        self.n += 1
        await self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})

    async def js(self, expr: str):
        r = await self.send(
            "Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True
        )
        return r.get("result", {}).get("value")

    async def goto(self, url: str, wait: float = 1.5):
        await self.send("Page.navigate", url=url)
        await asyncio.sleep(wait)

    async def wait(self, expr: str, timeout: float = 90):
        end = time.time() + timeout
        while time.time() < end:
            if await self.js(expr):
                return
            await asyncio.sleep(0.5)
        raise TimeoutError(expr)

    async def shot(self, name: str, full: bool = False):
        params = {"format": "png"}
        if full:
            h = await self.js("document.documentElement.scrollHeight")
            await self.send(
                "Emulation.setDeviceMetricsOverride",
                width=WIDTH,
                height=max(HEIGHT, int(h)),
                deviceScaleFactor=1,
                mobile=False,
            )
            await asyncio.sleep(0.5)
        data = (await self.send("Page.captureScreenshot", **params))["data"]
        (IMAGES / f"{name}.png").write_bytes(base64.b64decode(data))
        if full:
            await self.viewport()
        print(f"  docs/images/{name}.png")

    async def viewport(self):
        await self.send(
            "Emulation.setDeviceMetricsOverride",
            width=WIDTH,
            height=HEIGHT,
            deviceScaleFactor=1,
            mobile=False,
        )

    async def ask(self, message: str, pending_shot: str | None = None):
        await self.js(
            f"""(() => {{
              const i = document.getElementById('chat-input');
              i.value = {json.dumps(message)};
              document.getElementById('chat-form').requestSubmit();
            }})()"""
        )
        if pending_shot:
            await self.wait("document.querySelector('.pending-text')?.textContent.includes('호출 중')", 60)
            await self.shot(pending_shot)
        await self.wait("!document.getElementById('chat-send').disabled", 120)
        await asyncio.sleep(1)

    async def new_session(self):
        await self.js("document.getElementById('new-session').click()")
        await asyncio.sleep(0.3)


def chrome() -> tuple[subprocess.Popen, str, str]:
    exe = next((p for p in CHROME if p.exists()), None)
    if exe is None:
        raise RuntimeError("Chrome 또는 Edge를 찾지 못했습니다.")
    profile = tempfile.mkdtemp(prefix="capture-")
    proc = subprocess.Popen(
        [
            str(exe),
            "--headless=new",
            f"--remote-debugging-port={DEBUG_PORT}",
            f"--user-data-dir={profile}",
            f"--window-size={WIDTH},{HEIGHT}",
            "--lang=ko-KR",
            "--hide-scrollbars",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(40):
        try:
            tabs = requests.get(f"http://127.0.0.1:{DEBUG_PORT}/json", timeout=1).json()
            page = next(t for t in tabs if t["type"] == "page")
            return proc, page["webSocketDebuggerUrl"], profile
        except (requests.RequestException, StopIteration):
            time.sleep(0.5)
    proc.kill()
    raise RuntimeError("Chrome 디버깅 포트에 연결하지 못했습니다.")


async def capture(ws_url: str):
    base = f"http://127.0.0.1:{PORT}"
    async with websockets.connect(ws_url, max_size=50_000_000) as ws:
        page = Page(ws)
        await page.send("Page.enable")
        await page.send("Runtime.enable")
        await page.viewport()

        # 4.5 F3017: FastAPI 문서 화면
        await page.goto(f"{base}/docs", 3)
        await page.shot("api_docs", full=True)

        # 4.2, 4.5 F3019: 대표 요청 (처리 중 → 답변, 실행 단계, Tool 호출 이력)
        await page.goto(f"{base}/#chat")
        await page.ask(REPRESENTATIVE, pending_shot="chat_pending")
        await page.shot("chat_result")

        # 4.4 F3015: 같은 날짜에 시간이 겹치는 계획을 먼저 저장해 두고, 화면의 초안으로 저장
        assert await page.js("!!document.querySelector('form.draft')"), "계획 초안이 없습니다."
        requests.post(
            f"{base}/api/plans",
            json={
                "client": "B사",
                "product": "샤프트",
                "item": "철근및봉강",
                "meeting_date": "2026-11-25",
                "start_time": "14:30",
                "end_time": "15:30",
            },
            timeout=10,
        ).raise_for_status()
        await page.js(
            """(() => {
              const f = document.querySelector('form.draft');
              f.meeting_date.value = '2026-11-25';
              f.start_time.value = '14:00';
              f.end_time.value = '15:00';
              f.scrollIntoView({block: 'center'});
              f.requestSubmit();
            })()"""
        )
        await page.wait("document.querySelector('form.draft.is-saved .draft-result')?.textContent", 15)
        await page.js("document.querySelector('form.draft .draft-result').scrollIntoView({block: 'end'})")
        await asyncio.sleep(0.5)
        await page.shot("plan_saved_overlap")

        # 4.2, 4.5 F3009, F3019: 재계획 지점 구분
        await page.new_session()
        await page.ask(REPLAN)
        await page.shot("chat_replan")

        # 4.4 F3015, F3016: 계획 관리 화면 (목록, 상세)
        await page.goto(f"{base}/#plans")
        await page.js("document.querySelector('#plan-rows tr')?.click()")
        await asyncio.sleep(1)
        await page.shot("plans")

        # 4.3 F3014, F3020: 실행 로그 화면 (세션 목록, 실행 순서 상세, 판정)
        await page.goto(f"{base}/#logs")
        await page.js("document.querySelector('#session-list .session:last-child')?.click()")
        await asyncio.sleep(1.5)
        await page.shot("logs")

        # 4.5 F3019, NF3002: 외부 API 키 누락 (설정 안내, Tool 실패 안내)
        await page.goto(f"http://127.0.0.1:{NOKEY_PORT}/#chat")
        await page.ask(REPRESENTATIVE)
        await page.shot("chat_missing_key")


def main() -> int:
    IMAGES.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)
    blank = {"ECOS_OPEN_API": "", "KEP_CO_API": "", "LAW_API_OC": ""}
    procs = [
        server(PORT, DATA / "screens.db"),
        server(NOKEY_PORT, DATA / "screens_nokey.db", blank),
    ]
    browser, ws_url, profile = chrome()
    procs.append(browser)
    try:
        asyncio.run(capture(ws_url))
    finally:
        for p in procs:
            p.kill()
        time.sleep(1)
        shutil.rmtree(profile, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
