#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""batch_concurrency_test.py — OCR Gateway 동시처리/배치 부하 테스트.

계약 출처: "OCR Gateway API — PaddleOCR-VL". 게이트웨이는 MAX_CONCURRENT(기본 8)로
요청을 병렬 처리하고, vLLM(:8104)이 continuous batching 으로 GPU 에서 함께 소화한다.
이 스크립트는 파일 N개를 동시에 던져 실제 처리량·지연·동시성을 측정한다.

특징:
  - Python3 표준 라이브러리만 사용(urllib, concurrent.futures) → 폐쇄망 안전(pip 불요).
  - sync 모드: POST /ocr/{engine} 를 C개 동시 실행.
  - async 모드: POST /ocr/{engine}/tasks 로 전부 제출 → 폴링 → /result 수거.
  - 실행 중 GET /health 를 샘플링해 kv_cache 사용률·device 를 함께 보고.

사용:
  # doc.pdf 를 24회, 동시 8로 동기 부하
  ./batch_concurrency_test.py doc.pdf --repeat 24 --concurrency 8

  # 여러 파일을 비동기로
  ./batch_concurrency_test.py a.pdf b.png c.pdf --mode async --concurrency 12

  # 원격 + 차트 인식 opts
  ./batch_concurrency_test.py doc.pdf --host 10.0.0.5:18081 --opts '{"use_chart_recognition": true}'
"""
from __future__ import annotations
import argparse, io, json, mimetypes, os, sys, threading, time, uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib import request, error

# ── 멀티파트 인코더 (requests 없이 stdlib 로) ─────────────────────────────────
def build_multipart(fields: dict, file_field: str, path: str) -> tuple[str, bytes]:
    boundary = "----ocrbatch" + uuid.uuid4().hex
    fname = os.path.basename(path)
    ctype = mimetypes.guess_type(fname)[0] or "application/octet-stream"
    with open(path, "rb") as f:
        data = f.read()
    buf = io.BytesIO()
    for k, v in fields.items():
        if v is None:
            continue
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode())
        buf.write(f"{v}\r\n".encode())
    buf.write(f"--{boundary}\r\n".encode())
    buf.write(f'Content-Disposition: form-data; name="{file_field}"; filename="{fname}"\r\n'.encode())
    buf.write(f"Content-Type: {ctype}\r\n\r\n".encode())
    buf.write(data)
    buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())
    return boundary, buf.getvalue()


def http(method: str, url: str, *, body: bytes = None, boundary: str = None, timeout: float = 600):
    """반환: (http_code, parsed_json_or_None, raw_text). 4xx/5xx 도 예외 없이 코드로."""
    headers = {}
    if boundary:
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    req = request.Request(url, data=body, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            code = r.getcode()
    except error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        code = e.code
    except Exception as e:  # 연결거부·타임아웃 등
        return 0, None, str(e)
    try:
        return code, json.loads(raw), raw
    except Exception:
        return code, None, raw


# ── /health 백그라운드 샘플러 (동시성/메모리 관찰) ────────────────────────────
class HealthSampler(threading.Thread):
    def __init__(self, base, interval=2.0):
        super().__init__(daemon=True)
        self.base, self.interval = base, interval
        self._stop = threading.Event()
        self.max_kv = None
        self.device = None
    def run(self):
        while not self._stop.is_set():
            code, j, _ = http("GET", f"{self.base}/health", timeout=8)
            if j:
                kv = j.get("kv_cache_usage_perc")
                if isinstance(kv, (int, float)):
                    self.max_kv = kv if self.max_kv is None else max(self.max_kv, kv)
                self.device = j.get("device") or self.device
            self._stop.wait(self.interval)
    def stop(self):
        self._stop.set()


def pct(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    i = min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1))))
    return s[i]


# ── 단건 실행 (sync) ──────────────────────────────────────────────────────────
def run_sync(base, engine, path, fields, timeout):
    boundary, body = build_multipart(fields, "file", path)
    t0 = time.time()
    code, j, raw = http("POST", f"{base}/ocr/{engine}", body=body, boundary=boundary, timeout=timeout)
    wall = time.time() - t0
    ok = code == 200 and (j or {}).get("status") == "ok"
    server = (j or {}).get("metrics", {}).get("elapsed_s") if j else None
    err = None if ok else ((j or {}).get("error") or f"HTTP {code}" or raw[:120])
    return {"file": os.path.basename(path), "ok": ok, "code": code, "wall": wall,
            "server": server, "error": err}


# ── 단건 실행 (async: 제출만; 폴링은 별도) ────────────────────────────────────
def submit_async(base, engine, path, fields, timeout):
    boundary, body = build_multipart(fields, "file", path)
    t0 = time.time()
    code, j, raw = http("POST", f"{base}/ocr/{engine}/tasks", body=body, boundary=boundary, timeout=timeout)
    tid = (j or {}).get("task_id")
    return {"file": os.path.basename(path), "submit_code": code, "task_id": tid,
            "t_submit": t0, "ok": bool(tid) and code in (200, 202)}


def poll_result(base, engine, task, poll_timeout, interval):
    tid = task["task_id"]
    deadline = time.time() + poll_timeout
    st = "unknown"
    while time.time() < deadline:
        code, j, _ = http("GET", f"{base}/ocr/{engine}/tasks/{tid}", timeout=15)
        st = (j or {}).get("status", st)
        if st in ("completed", "failed"):
            break
        time.sleep(interval)
    wall = time.time() - task["t_submit"]
    if st != "completed":
        return {**task, "ok": False, "wall": wall, "server": None, "error": f"task {st}"}
    code, j, _ = http("GET", f"{base}/ocr/{engine}/tasks/{tid}/result", timeout=30)
    ok = code == 200 and (j or {}).get("status") == "ok"
    server = (j or {}).get("metrics", {}).get("elapsed_s") if j else None
    return {**task, "ok": ok, "wall": wall, "server": server,
            "error": None if ok else ((j or {}).get("error") or f"result HTTP {code}")}


def main():
    ap = argparse.ArgumentParser(description="OCR Gateway 동시처리/배치 테스트")
    ap.add_argument("files", nargs="+", help="입력 파일 1개 이상 (pdf/png/jpg/…)")
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1:18081"))
    ap.add_argument("--engine", default="paddleocr_vl")
    ap.add_argument("--lang", default="korean")
    ap.add_argument("--opts", default=None, help='엔진 opts JSON 문자열')
    ap.add_argument("--repeat", type=int, default=1, help="파일 목록을 반복해 총 N건으로")
    ap.add_argument("--concurrency", "-c", type=int, default=8)
    ap.add_argument("--mode", choices=["sync", "async"], default="sync")
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--poll-timeout", type=float, default=900)
    ap.add_argument("--poll-interval", type=float, default=3)
    args = ap.parse_args()

    base = f"http://{args.host}"
    for f in args.files:
        if not os.path.isfile(f):
            print(f"파일 없음: {f}", file=sys.stderr); sys.exit(2)
    # 작업 목록 = 파일들 × repeat
    work = [args.files[i % len(args.files)] for i in range(max(args.repeat, len(args.files)))]
    if args.repeat < len(args.files):
        work = list(args.files)
    fields = {"lang": args.lang, "opts": args.opts}

    print(f"\033[1;36mOCR 동시처리 테스트\033[0m base={base} engine={args.engine} "
          f"mode={args.mode} 동시={args.concurrency} 총건수={len(work)}")

    # 프리플라이트 health
    code, j, _ = http("GET", f"{base}/health", timeout=10)
    if code != 200:
        print(f"\033[1;31m✗ /health {code} — 게이트웨이 미기동?\033[0m"); sys.exit(1)
    print(f"  health OK · device={ (j or {}).get('device') } "
          f"· kv_cache={ (j or {}).get('kv_cache_usage_perc') }%")

    sampler = HealthSampler(base); sampler.start()
    results = []
    wall0 = time.time()

    if args.mode == "sync":
        with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            futs = [ex.submit(run_sync, base, args.engine, p, fields, args.timeout) for p in work]
            for i, fut in enumerate(as_completed(futs), 1):
                r = fut.result(); results.append(r)
                mark = "\033[1;32m✓\033[0m" if r["ok"] else "\033[1;31m✗\033[0m"
                print(f"  [{i}/{len(work)}] {mark} {r['file']} wall={r['wall']:.1f}s "
                      f"server={r['server']}s{'' if r['ok'] else '  ERR: '+str(r['error'])[:80]}")
    else:  # async: 동시 제출 → 동시 폴링
        with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            tasks = list(ex.map(lambda p: submit_async(base, args.engine, p, fields, args.timeout), work))
        subok = [t for t in tasks if t["ok"]]
        print(f"  제출 완료: {len(subok)}/{len(tasks)} (202/200)")
        with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            futs = [ex.submit(poll_result, base, args.engine, t, args.poll_timeout, args.poll_interval)
                    for t in subok]
            for i, fut in enumerate(as_completed(futs), 1):
                r = fut.result(); results.append(r)
                mark = "\033[1;32m✓\033[0m" if r["ok"] else "\033[1;31m✗\033[0m"
                print(f"  [{i}/{len(subok)}] {mark} {r['file']} wall={r['wall']:.1f}s "
                      f"server={r['server']}s{'' if r['ok'] else '  ERR: '+str(r['error'])[:80]}")
        for t in tasks:
            if not t["ok"]:
                results.append({"file": t["file"], "ok": False, "wall": 0,
                                "server": None, "error": f"submit {t['submit_code']}"})

    wall = time.time() - wall0
    sampler.stop()

    ok = [r for r in results if r["ok"]]
    walls = [r["wall"] for r in ok]
    servers = [r["server"] for r in ok if isinstance(r["server"], (int, float))]
    n = len(results); nok = len(ok); nfail = n - nok

    print("\n\033[1;36m─────────── 결과 요약 ───────────\033[0m")
    print(f"  총 {n}건 · 성공 {nok} · 실패 {nfail}")
    print(f"  전체 소요(wall)      : {wall:.1f}s")
    print(f"  처리량(throughput)   : {nok / wall:.2f} docs/s  ({nok / wall * 60:.1f} docs/min)")
    if walls:
        print(f"  요청 지연(클라 wall) : p50={pct(walls,50):.1f}s  p95={pct(walls,95):.1f}s  "
              f"max={max(walls):.1f}s  avg={sum(walls)/len(walls):.1f}s")
    if servers:
        print(f"  서버 처리(elapsed_s) : avg={sum(servers)/len(servers):.1f}s  max={max(servers):.1f}s")
    print(f"  kv_cache 최대 사용률  : {sampler.max_kv}%  · device={sampler.device}")
    if sampler.max_kv is not None and sampler.max_kv >= 95:
        print("  \033[1;33m! kv_cache 가득참 — 동시성 낮추거나 PADDLE_UTIL 상향(문서 튜닝 주의)\033[0m")
    if nfail:
        print("  \033[1;33m실패 표본:\033[0m", "; ".join(f"{r['file']}:{str(r['error'])[:40]}" for r in results if not r["ok"])[:200])

    sys.exit(0 if nfail == 0 else 1)


if __name__ == "__main__":
    main()
