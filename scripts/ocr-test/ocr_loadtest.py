#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ocr_loadtest.py — OCR Gateway 램프(step) 부하 테스트: '어디까지 버티나'를 찾는다.

동시성을 단계적으로 올리며(예: 2→4→8→12→16) 각 단계에서 요청을 몰아넣고
성공률·처리량·지연(p50/p95/max)·kv_cache 최대치를 잰다. 성공률이 임계 아래로
떨어지면(=무너짐) 그 직전을 '최대 안정 동시성'으로 리포트하고 중단한다.

이 시스템은 GPU/vLLM 단일 백엔드라 처리량은 금방 포화한다(동시 올려도 pages/min 은
안 늘고 지연만 늘어남). 따라서 한계 = 성공률이 떨어지거나 p95 가 폭발하는 동시성이다.

Python3 표준 라이브러리만 사용(pip 불요). 게이트웨이는 PDF/이미지를 그대로 받는다.

사용:
  # test_doc 폴더로 2,4,8,12,16 단계 램프
  ./ocr_loadtest.py ../../test_doc --host api-doc.ys-helperai.com --steps 2,4,8,12,16

  # 단일 이미지로, 각 단계 요청수 지정
  ./ocr_loadtest.py /tmp/ocr_page1.jpg --steps 4,8,16,24 --count-per-step 24

옵션:
  --steps           동시성 단계(콤마). 기본 "2,4,8,12,16"
  --count-per-step  단계당 총 요청수. 미지정 시 3×동시성
  --fail-threshold  이 성공률(0~1) 밑으로 떨어지면 중단. 기본 0.95
  --p95-abort-s     p95 지연이 이 값(초) 넘으면 중단. 기본 없음(0=무시)
  --host --engine --lang --opts --timeout --poll-timeout
"""
from __future__ import annotations
import argparse, io, json, mimetypes, os, sys, threading, time, uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib import request, error

EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tiff", ".tif", ".bmp"}
UA = os.environ.get("OCR_UA", "ocr-loadtest/1.0 (curl-like)")


def build_multipart(fields, file_field, path):
    boundary = "----ocrload" + uuid.uuid4().hex
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
    buf.write(data); buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())
    return boundary, buf.getvalue()


def http(method, url, *, body=None, boundary=None, timeout=600):
    headers = {"User-Agent": UA}
    if boundary:
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    req = request.Request(url, data=body, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            code = r.getcode()
    except error.HTTPError as e:
        raw, code = e.read(), e.code
    except Exception as e:
        return 0, {"_err": str(e)}
    try:
        return code, json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return code, None


def pct(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(p / 100.0 * (len(s) - 1))))]


class HealthSampler(threading.Thread):
    def __init__(self, base, interval=2.0):
        super().__init__(daemon=True)
        self.base, self.interval, self._stop = base, interval, threading.Event()
        self.max_kv = None
    def run(self):
        while not self._stop.is_set():
            _, j = http("GET", f"{self.base}/health", timeout=8)
            if j and isinstance(j.get("kv_cache_usage_perc"), (int, float)):
                self.max_kv = max(self.max_kv or 0, j["kv_cache_usage_perc"])
            self._stop.wait(self.interval)
    def stop(self): self._stop.set()


def process_one(base, engine, path, fields, poll_timeout, interval):
    t0 = time.time()
    boundary, body = build_multipart(fields, "file", path)
    code, j = http("POST", f"{base}/ocr/{engine}/tasks", body=body, boundary=boundary, timeout=120)
    tid = (j or {}).get("task_id")
    if not tid or code not in (200, 202):
        return {"ok": False, "wall": time.time() - t0, "pages": 0,
                "error": f"제출 {code} {((j or {}).get('_err') or '')}"[:100]}
    st, deadline = "unknown", time.time() + poll_timeout
    while time.time() < deadline:
        code, j = http("GET", f"{base}/ocr/{engine}/tasks/{tid}", timeout=15)
        st = (j or {}).get("status", st)
        if st in ("completed", "failed"):
            break
        time.sleep(interval)
    wall = time.time() - t0
    _, jr = http("GET", f"{base}/ocr/{engine}/tasks/{tid}/result", timeout=30)
    ok = st == "completed" and (jr or {}).get("status") == "ok"
    return {"ok": ok, "wall": wall, "pages": len((jr or {}).get("layout") or []),
            "error": None if ok else (str((jr or {}).get("error"))[:120] if jr and jr.get("error") else f"task {st}")}


def run_step(base, engine, work, concurrency, fields, poll_timeout, interval):
    sampler = HealthSampler(base); sampler.start()
    results = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = [ex.submit(process_one, base, engine, p, fields, poll_timeout, interval) for p in work]
        for fut in as_completed(futs):
            results.append(fut.result())
    wall = time.time() - t0
    sampler.stop()
    ok = [r for r in results if r["ok"]]
    walls = [r["wall"] for r in ok]
    return {
        "n": len(results), "ok": len(ok), "fail": len(results) - len(ok),
        "success": len(ok) / len(results) if results else 0,
        "wall": wall,
        "docs_min": len(ok) / wall * 60 if wall else 0,
        "pages_min": sum(r["pages"] for r in ok) / wall * 60 if wall else 0,
        "p50": pct(walls, 50), "p95": pct(walls, 95), "max": max(walls) if walls else 0,
        "kv": sampler.max_kv,
        "errors": [r["error"] for r in results if not r["ok"]],
    }


def main():
    ap = argparse.ArgumentParser(description="OCR Gateway 램프 부하 테스트(한계 탐색)")
    ap.add_argument("path", help="파일 또는 폴더")
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1:18081"))
    ap.add_argument("--engine", default="paddleocr_vl")
    ap.add_argument("--lang", default="korean")
    ap.add_argument("--opts", default=None)
    ap.add_argument("--steps", default="2,4,8,12,16", help="동시성 단계(콤마)")
    ap.add_argument("--count-per-step", type=int, default=0, help="단계당 요청수(0=3×동시성)")
    ap.add_argument("--fail-threshold", type=float, default=0.95, help="이 성공률 밑이면 중단")
    ap.add_argument("--p95-abort-s", type=float, default=0.0, help="p95 이 값(초) 넘으면 중단(0=무시)")
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--poll-timeout", type=float, default=1800)
    ap.add_argument("--poll-interval", type=float, default=3)
    args = ap.parse_args()

    base = args.host if args.host.startswith(("http://", "https://")) else f"http://{args.host}"
    if os.path.isdir(args.path):
        pool = sorted(os.path.join(args.path, f) for f in os.listdir(args.path)
                      if os.path.splitext(f)[1].lower() in EXTS)
    elif os.path.isfile(args.path):
        pool = [args.path]
    else:
        print(f"경로 없음: {args.path}", file=sys.stderr); sys.exit(2)
    if not pool:
        print("지원 파일 없음", file=sys.stderr); sys.exit(2)
    steps = [int(x) for x in args.steps.split(",") if x.strip()]
    fields = {"lang": args.lang, "opts": args.opts}

    print(f"\033[1;36mOCR 램프 부하 테스트\033[0m base={base} engine={args.engine}")
    print(f"  입력={args.path} ({len(pool)}개 소스) · 단계={steps} · 중단임계 성공률<{args.fail_threshold:.0%}"
          + (f" 또는 p95>{args.p95_abort_s:.0f}s" if args.p95_abort_s else ""))
    code, j = http("GET", f"{base}/health", timeout=15)
    if code != 200:
        print(f"\033[1;31m✗ /health {code} — 게이트웨이 미기동?\033[0m"); sys.exit(1)
    print(f"  health OK · device={ (j or {}).get('device') }\n")

    print(f"  {'동시':>4} {'요청':>4} {'성공률':>7} {'docs/min':>9} {'pages/min':>10} "
          f"{'p50':>6} {'p95':>7} {'max':>7} {'kv%':>5}")
    print("  " + "─" * 66)

    rows = []
    max_stable = None
    for c in steps:
        count = args.count_per_step or max(len(pool), 3 * c)
        work = [pool[i % len(pool)] for i in range(count)]
        r = run_step(base, args.engine, work, c, fields, args.poll_timeout, args.poll_interval)
        rows.append((c, r))
        col = "\033[1;32m" if r["success"] >= args.fail_threshold else "\033[1;31m"
        print(f"  {c:>4} {r['n']:>4} {col}{r['success']*100:>6.1f}%\033[0m "
              f"{r['docs_min']:>9.1f} {r['pages_min']:>10.1f} "
              f"{r['p50']:>5.0f}s {r['p95']:>6.0f}s {r['max']:>6.0f}s {str(r['kv']):>5}")
        broke = r["success"] < args.fail_threshold or (args.p95_abort_s and r["p95"] > args.p95_abort_s)
        if broke:
            print(f"  \033[1;33m→ 동시성 {c} 에서 무너짐 (성공률 {r['success']*100:.1f}%, p95 {r['p95']:.0f}s)\033[0m")
            from collections import Counter
            for e, cnt in Counter(r["errors"]).most_common(3):
                print(f"      {cnt}건 · {e}")
            break
        max_stable = c

    print("\n\033[1;36m─────────── 한계 판정 ───────────\033[0m")
    if max_stable is not None:
        best = max((r for _, r in rows if r["success"] >= args.fail_threshold),
                   key=lambda r: r["pages_min"], default=None)
        print(f"  ✅ 최대 안정 동시성 : \033[1;32m{max_stable}\033[0m (성공률 ≥{args.fail_threshold:.0%})")
        if best:
            print(f"  처리량 포화       : ≈ {best['pages_min']:.1f} pages/min ({best['docs_min']:.1f} docs/min)")
        nextstep = next((c for c, r in rows if r["success"] < args.fail_threshold), None)
        if nextstep:
            print(f"  무너지는 동시성    : {nextstep} 부터 (성공률 급락)")
        else:
            print(f"  \033[1;33m주어진 단계({steps})까지는 안 무너짐 — 더 높은 단계로 재시도해 한계 탐색.\033[0m")
    else:
        print("  \033[1;31m첫 단계부터 실패 — 게이트웨이/모델 상태 점검 필요.\033[0m")
    sys.exit(0)


if __name__ == "__main__":
    main()
