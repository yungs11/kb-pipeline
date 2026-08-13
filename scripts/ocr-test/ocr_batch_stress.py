#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ocr_batch_stress.py — OCR Gateway 배치/동시처리 '안정성' 테스트.

목적: 클라이언트가 배치를 만드는 게 아니라, **OCR 게이트웨이가 동시/배치 부하를 얼마나
안정적으로 소화하는지**를 본다. 테스트 폴더의 문서들을 한꺼번에(동시 C건) 게이트웨이에
던지고, 게이트웨이(MAX_CONCURRENT) + vLLM continuous batching 이 흔들리지 않는지 —
성공률·지연 안정성·실패 유무를 측정한다. --rounds 로 같은 폴더를 반복해 지속 부하를 준다.

Python3 표준 라이브러리만 사용(pip 불요). 게이트웨이는 PDF/이미지를 그대로 받는다.

사용:
  # test_doc 폴더 전체를 동시 8로 1회
  ./ocr_batch_stress.py ../../test_doc --host 15.164.81.29:18081 -c 8

  # 지속 부하: 폴더를 5회 반복(내구성 확인)
  ./ocr_batch_stress.py ../../test_doc --host 15.164.81.29:18081 -c 8 --rounds 5

  # 동시성 올려 한계 탐색
  ./ocr_batch_stress.py ./docs -c 16
"""
from __future__ import annotations
import argparse, io, json, mimetypes, os, sys, time, uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib import request, error

EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tiff", ".tif", ".bmp"}
UA = os.environ.get("OCR_UA", "ocr-batch-stress/1.0 (curl-like)")


def build_multipart(fields, file_field, path):
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
            return r.getcode(), _json(r.read())
    except error.HTTPError as e:
        return e.code, _json(e.read())
    except Exception as e:
        return 0, {"_err": str(e)}


def _json(raw):
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None


def pct(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(p / 100.0 * (len(s) - 1))))]


def process_one(base, engine, path, fields, poll_timeout, interval):
    """비동기: 제출 → 폴링 → result. 반환: 안정성 측정용 dict."""
    t0 = time.time()
    boundary, body = build_multipart(fields, "file", path)
    code, j = http("POST", f"{base}/ocr/{engine}/tasks", body=body, boundary=boundary, timeout=120)
    tid = (j or {}).get("task_id")
    if not tid or code not in (200, 202):
        return {"file": os.path.basename(path), "ok": False, "wall": time.time() - t0,
                "pages": 0, "server": None, "error": f"제출 {code} {((j or {}).get('_err') or '')}"[:120]}
    st = "unknown"
    deadline = time.time() + poll_timeout
    while time.time() < deadline:
        code, j = http("GET", f"{base}/ocr/{engine}/tasks/{tid}", timeout=15)
        st = (j or {}).get("status", st)
        if st in ("completed", "failed"):
            break
        time.sleep(interval)
    wall = time.time() - t0
    if st != "completed":
        # 실패/타임아웃이어도 result 에서 실제 사유(예: vlm worker connection error)를 회수
        _, jr = http("GET", f"{base}/ocr/{engine}/tasks/{tid}/result", timeout=30)
        real = str((jr or {}).get("error"))[:200] if jr and jr.get("error") else None
        return {"file": os.path.basename(path), "ok": False, "wall": wall, "pages": 0,
                "server": None, "error": real or f"task {st}"}
    code, j = http("GET", f"{base}/ocr/{engine}/tasks/{tid}/result", timeout=30)
    ok = code == 200 and (j or {}).get("status") == "ok"
    return {"file": os.path.basename(path), "ok": ok, "wall": wall,
            "pages": len((j or {}).get("layout") or []),
            "server": (j or {}).get("metrics", {}).get("elapsed_s") if j else None,
            "error": None if ok else str((j or {}).get("error"))[:160]}


def main():
    ap = argparse.ArgumentParser(description="OCR Gateway 배치/동시처리 안정성 테스트")
    ap.add_argument("folder", nargs="?", default="../../test_doc", help="문서 폴더(기본 ../../test_doc)")
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1:18081"))
    ap.add_argument("--engine", default="paddleocr_vl")
    ap.add_argument("--lang", default="korean")
    ap.add_argument("--opts", default=None)
    ap.add_argument("--concurrency", "-c", type=int, default=8, help="동시 in-flight(게이트웨이 MAX_CONCURRENT 자극)")
    ap.add_argument("--rounds", type=int, default=1, help="폴더 반복 횟수(지속 부하)")
    ap.add_argument("--poll-timeout", type=float, default=1800)
    ap.add_argument("--poll-interval", type=float, default=3)
    args = ap.parse_args()

    base = args.host if args.host.startswith(("http://", "https://")) else f"http://{args.host}"
    files = sorted(os.path.join(args.folder, f) for f in os.listdir(args.folder)
                   if os.path.splitext(f)[1].lower() in EXTS) if os.path.isdir(args.folder) else []
    if not files:
        print(f"폴더에 지원 파일 없음: {args.folder}  (지원: {', '.join(sorted(EXTS))})", file=sys.stderr)
        sys.exit(2)
    work = files * args.rounds
    fields = {"lang": args.lang, "opts": args.opts}

    print(f"\033[1;36mOCR 배치 안정성 테스트\033[0m  base={base} engine={args.engine}")
    print(f"  폴더={args.folder}  파일={len(files)}개 × {args.rounds}회 = {len(work)}건  동시={args.concurrency}")
    code, j = http("GET", f"{base}/health", timeout=15)
    if code != 200:
        print(f"\033[1;31m✗ /health {code} — 게이트웨이 미기동?\033[0m"); sys.exit(1)
    print(f"  health OK · device={ (j or {}).get('device') } · loaded={ (j or {}).get('loaded') }")

    results = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = {ex.submit(process_one, base, args.engine, p, fields, args.poll_timeout, args.poll_interval): p
                for p in work}
        for i, fut in enumerate(as_completed(futs), 1):
            r = fut.result(); r["order"] = i; results.append(r)
            mark = "\033[1;32m✓\033[0m" if r["ok"] else "\033[1;31m✗\033[0m"
            print(f"  [{i}/{len(work)}] {mark} {r['file']}  wall={r['wall']:.1f}s "
                  f"pages={r['pages']} server={r['server']}s"
                  f"{'' if r['ok'] else '  ERR: '+str(r['error'])[:90]}")
    wall = time.time() - t0

    ok = [r for r in results if r["ok"]]
    fails = [r for r in results if not r["ok"]]
    walls = [r["wall"] for r in ok]
    servers = [r["server"] for r in ok if isinstance(r["server"], (int, float))]
    n = len(results)

    print("\n\033[1;36m─────────── 안정성 요약 ───────────\033[0m")
    print(f"  총 {n}건 · 성공 {len(ok)} · 실패 {len(fails)}  → 성공률 {100*len(ok)/n:.1f}%")
    print(f"  전체 소요(wall)   : {wall:.1f}s")
    print(f"  처리량            : {len(ok)/wall*60:.1f} docs/min  ({sum(r['pages'] for r in ok)/wall*60:.1f} pages/min)")
    if walls:
        print(f"  요청 지연         : min={min(walls):.1f}s  p50={pct(walls,50):.1f}s  "
              f"p95={pct(walls,95):.1f}s  max={max(walls):.1f}s")
    if servers:
        print(f"  서버 처리(elapsed): avg={sum(servers)/len(servers):.1f}s  max={max(servers):.1f}s")

    # 안정성 신호: 완료 순서 앞/뒤 1/3 지연 비교 → 뒤가 크게 늘면 부하로 열화
    if len(walls) >= 6:
        k = max(1, len(ok) // 3)
        early = [r["wall"] for r in sorted(ok, key=lambda x: x["order"])[:k]]
        late = [r["wall"] for r in sorted(ok, key=lambda x: x["order"])[-k:]]
        ea, la = sum(early)/len(early), sum(late)/len(late)
        drift = (la - ea) / ea * 100 if ea else 0
        tag = "\033[1;32m안정\033[0m" if drift < 40 else "\033[1;33m열화 조짐\033[0m"
        print(f"  지연 추이         : 초기 {ea:.1f}s → 후기 {la:.1f}s ({drift:+.0f}%)  [{tag}]")

    if fails:
        print("\n  \033[1;33m실패 상세:\033[0m")
        from collections import Counter
        for reason, c in Counter(f["error"] for f in fails).most_common():
            print(f"    {c}건 · {reason}")
        print("  \033[1;33m→ 실패가 있으면 동시성을 낮추거나(예: -c 4) 게이트웨이 로그 확인.\033[0m")
    else:
        print("\n  \033[1;32m✅ 전건 성공 — 이 동시성/부하에서 게이트웨이 안정.\033[0m")

    sys.exit(0 if not fails else 1)


if __name__ == "__main__":
    main()
