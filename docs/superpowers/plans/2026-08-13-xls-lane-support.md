<!-- plan-version: v6 -->
<!-- ultracode-validation: READY v6 at 2026-08-13T05:49:30Z -->

# 레거시 `.xls` 지원 — 레인 입구에서 `.xlsx` 로 변환(LibreOffice)

> v1 → v2: ultracode 4렌즈 must-fix 15건 반영 — (a) V1~V4 회귀선이 "실행 불가 아니면 항상
> 통과" 였던 것을 관측 가능한 심볼 기준으로 재작성, (b) 확장자 100% 판정 → **매직바이트
> 게이트**, (c) soffice 프로필/고아 프로세스 수명 재설계, (d) 게이트 `#REF!` 뒤집힘 축
> 추가, (e) 임시물 누적 차단, (f) 인용 좌표 정정.
>
> v2 → v3: 2라운드(2렌즈) must-fix 6건 반영 — (a) **v2 가 새로 만든 `.xlsm` 회귀 제거**
> (zip 매직 분기가 `.xlsm` 를 `.xlsx` 로 덮어썼다), (b) 누수 assert 를 **증명력 있는
> 자리로 이동**(v1 #3 재발 — fake 로 대체된 함수는 tmpdir 을 만들지 않으므로 항상 통과),
> (c) `workbook_loader.py:39` 의 누수 원형 처리 명시, (d) 픽스처 재현성(생성 스크립트
> 커밋 + 캐시값 `#REF!` 실재 전제 assert), (e) CFB 매직의 과포함(구 doc/ppt·암호 OOXML)
> 문서화, (f) 문서 갱신 대상 정정(`build-bundle.sh:122-125` 는 용량 문구가 아니라 사고
> 기록이라 건드리지 않는다). 즉시 드러나는 항목 7건은 **`## 구현 후 검증`** 으로 이관.
>
> v3 → v4: 최종 전문 검증 must-fix 3건 — (a) §2.6 에 v2 이전 지시(`build-bundle.sh` 주석
> 갱신)가 남아 §3·F19 와 **문서 내부 모순**이었다(실행해도 안 드러나는 부류), (b) V2b 의
> 잔존 assert 3종 중 둘이 **또 vacuous**(스텁 상태에선 그 디렉터리가 생기지 않는다) → 실
> 변환이 도는 V3·V6 로 이관, (c) 어댑터가 **사용자 파일명을 디스크 이름으로** 써서
> `#`·`%` 든 한글 파일명에서 산출물 basename 이 어긋나면 현장에서만 죽는다 → 내부 고정
> 이름 + 산출물 탐색 폴백. 부가로 V1 ⑤ 문구 정정, `airgap-deploy.md` 앵커 제외,
> P6 미검증 명시 규칙, V3 에 kordoc 원본↔변환본 열화 비교 추가.
>
> v4 → v5: **같은 부류(vacuous assert)의 4번째 재발** 차단. §2.3-1 이 프로필을
> `<tmpdir>/lo_<uuid4>` 로 **중첩** 생성하는데 V3·V6 는 `gettempdir()` **직하**의 `lo_*` 를
> 세고 있었다 → 항상 `0 == 0`, R3(프로필 잔존 = 워커 영구 불능)을 여전히 아무도 관측하지
> 못했다. 프로필을 최상위 `tempfile.mkdtemp(prefix="lo_")` 로 고정하고, 폴백 탐색은 비재귀
> glob 으로 한정. `excel_parser_rag_xls_*` 는 개수 비교가 아니라 **"하나도 생기지 않는다"**
> (= 모든 호출처가 `output_dir` 규약을 지켰다)로 의미를 바꿨다. V6 에 타임아웃 유발 수단
> (모듈 속성 monkeypatch, 새 env 금지)도 명시.
>
> v5 → v6: **5번째 vacuous 재발** — 이번엔 plan 이 스스로 "최대 미검증 축"이라 부른
> §2.4(게이트 `#REF!` 뒤집힘) 한복판이었다. 게이트는 캐시값·수식 워크북을 하나의 `ref_set`
> 으로 합치므로(`excel_gate.py:190-196`), 수식 텍스트에 `#REF!` 토큰이 있는 통상 픽스처는
> 재계산 여부와 무관하게 **무조건** 잡혀 V3 ① 이 실패할 수 없었다. 픽스처를 **간접 참조**
> (`H3=<=J9>`, `J9=<=#REF!>` — 캐시값만 `#REF!`)로 못박고 V3 ① 을 `H3` 좌표 포함 여부로
> 바꿨다. 픽스처 전제 assert 도 **실행 불가능한 형태**였다(대상이 `.xls` 면 openpyxl 이 못
> 읽고, openpyxl 산 `.xlsx` 면 캐시값이 없다) → 생성 스크립트 안에서 soffice 로 되돌린 뒤
> (a) 캐시값 `#REF!` (b) 수식 텍스트에 토큰 없음 두 조건을 assert. V3 '병합' 항목의 모호한
> 표현도 `colspan` 개수/좌표로 못박았다.

## 0. 목표 / 비목표

**목표** — parse-svc 엑셀 레인이 레거시 `.xls`(BIFF)를 **`.xlsx` 와 동등하게** 처리한다.
값·병합셀·수식이 보존되어 전결(delegation)·계층(hierarchy) 라우팅과 게이트 검사가
`.xlsx` 와 같은 경로를 탄다. 호스트 dev·컨테이너·폐쇄망에서 **같은 코드로** 동작하고,
폐쇄망 회귀선(가드)이 이 기능을 실제로 검사한다.

**비목표(이번 범위 밖 — `deferred.md` 로 이관)**
- doc_guard 의 `.xls` 지원 — `SUPPORTED_EXTENSIONS = {".docx",".pdf",".xlsx"}`
  (`app/extract/__init__.py:10`), `.xls` 는 크래시가 아니라 **미지원 스킵**
- kb-backend `document_signals` 의 `.xls` 신호수집 — `_EXTRACTORS` 에 `xls` 키 없음 → 텍스트 폴백
  ⚠️ 위 둘은 **이 plan 배포 직후부터 비로소 도달 가능해진다**(지금은 parse 단계에서 먼저 죽어
  도달 불가). 배포 후 신호 degrade 가 관측 가능해지는 시점이 재검토 트리거다
- 파싱 전반의 동기 블로킹 → `run_in_threadpool` 전환 (§6 R2 근본 대응)
- 동시 `.xls` 변환 스트레스 테스트, libreoffice apt 의존 트리 버전 pin
- `.xlt`/`.xlsb` 등 그 밖의 레거시 포맷
- **`.xls` 이름 + 비CFB·비zip 바이트**(HTML 표·탭구분 텍스트를 `.xls` 로 저장한 것 — 실무에
  흔하다). 이번 범위는 **BIFF 개통**이다. 이런 입력은 매직바이트가 CFB 가 아니므로 변환을
  타지 않고 **오늘과 동일하게 실패**한다(V1 케이스 ⑤로 못박는다). "`.xlsx` 와 동등"이
  과장으로 읽히지 않게 여기 명시한다
- **암호화 OOXML 정밀 판별** — CFB 매직은 BIFF 전용이 아니다(구 `.doc`/`.ppt`, 암호화된
  `.xlsx` 도 CFB). 암호 `.xlsx` 는 지금은 openpyxl 이 즉시 실패하는데 변경 후엔 soffice
  변환 타임아웃(60s)까지 워커를 점유한 뒤 실패한다 → §6 R2 에 유입 경로로 명시하고,
  실제 유입이 관측되면 `EncryptedPackage` 스트림 탐지로 착수
- 이름만 `.xls` 인 zip(실체 xlsx)을 xlsx 레인으로 보내는 "더 올바른" 처리(§2.1 각주)

## 1. 실측 근거 (2026-08-13, 이 브랜치에서 직접 확인 — 좌표 v2 정정 완료)

| # | 사실 | 증거 |
|---|---|---|
| F1 | `.xls` 는 **호스트 맥에서도** 파싱이 죽는다(폐쇄망 한정 아님) | `parse(Path("src.xls").read_bytes(), "src.xls")` → `ParserError: excel parse failed for src.xls: openpyxl does not support the old .xls file format…` |
| F2 | 죽는 지점은 kordoc 백엔드의 **동반 openpyxl 읽기** | 트레이스백 `auto_backend.py:145 _kordoc()` → `kordoc_backend.py:836 wb = load_workbook(input_path, data_only=True)` |
| F3 | kordoc CLI 자체는 `.xls` 를 **정상 처리**한다 | 실 BIFF(soffice `MS Excel 97` 필터 생성)로 `kordoc src.xls -o out.md` → exit 0, `<table>`+`colspan` 정확 |
| F4 | 전결 `.xls` 는 openpyxl 레인으로 **가지 못한다** | `auto_backend.py:72-79` `_should_try_openpyxl` = `suffix in {".xlsx",".xlsm"}` **and** `detect_delegation_keyword(...)` — `and` 단락이라 `.xls` 는 :77 의 키워드 검사에 도달조차 못 함. Tier1.5(`:113-116`)도 같은 확장자 게이트 |
| F5 | 기존 변환기는 **실효적으로 죽은 코드**다 | `convert_xls_to_xlsx` 호출처는 `loaders/workbook_loader.py:39` 하나. 그 경로(`load_workbook_for_parsing`)는 openpyxl 백엔드 **외에** `pipeline.py:43`→`build_canvases`→게이트/계층 프로브에서도 쓰여 **도달 경로 자체는 있으나**, 그 전에 `kordoc_backend.py:836` 이 먼저 죽어 실효적으로 안 돈다 |
| F6 | 컨테이너에 soffice 가 **없다** | `Dockerfile.parse-svc` 는 java·node/kordoc 만 설치. compose·번들 어디에도 libreoffice 없음 |
| F7 | 호스트 맥에는 soffice 가 **있다**(`/usr/local/bin/soffice`) | "로컬만 되는" 착시의 소지 — 실제로는 F4 때문에 양쪽 다 죽음 |
| F8 | csv 는 이미 **레인 입구에서 바이트를 갈아끼우는** 선례가 있다 | `parsers/excel/__init__.py:63-68`(`is_csv` 판정 → `csv_bytes_to_xlsx`), `:69` 에서 `suffix` 를 `.xlsx` 로 고정 |
| F9 | 백엔드 `stats` 는 레인 밖으로 **나오지 않는다** | `__init__.py:88` `chunks, _stats = get_backend(...).parse(...)` 로 버림. `RouteResult`(`parsers/__init__.py:8-14`)에 stats 필드 없음 |
| F10 | 게이트 실패는 **예외가 아니라 값**으로 나온다 | `__init__.py:90-93` `except Exception → gate_summary = {"ok": False, "sheets": [], "error": str(exc)}` — 즉 `gate_summary is not None` 은 **항상 참** |
| F11 | 변환 실패가 `ParserError` 로 승격되는 지점 | `__init__.py:100-105` `parse()` 의 `except Exception as e: raise ParserError(f"excel parse failed for {filename}: {e}") from e`. `XlsConversionError` 는 `RuntimeError` 라 여기 걸린다 |
| F12 | `normalize_rag_chunk` 는 `chunk_type` 을 **버린다** | `{chunk_index,text,titles_context,pages}` 만 남김 → 레인 밖에서 `chunk_type` assert 불가 |
| F13 | `convert_xls_to_xlsx` 는 `output_dir` 미지정 시 **스스로 mkdtemp** 하고 정리하지 않는다 | `xls_converter.py:65` `tempfile.mkdtemp(prefix="excel_parser_rag_xls_")`, docstring "호출자가 정리 책임 없음" |
| F14 | `/parse` 는 워커당 **동시 1건**이다 | `app.py:447` `async def parse(...)` 가 `:460 run_parse(...)` 를 **동기 호출** → 이벤트루프 블로킹. gunicorn `-w 4` = 최대 4건 병렬 |
| F15 | 이미지에 테스트 픽스처가 **들어간다** | `.dockerignore` 는 `.venv/.venv-kb/__pycache__/*.pyc/.git/edgequake` 만 제외. `Dockerfile.parse-svc` 의 `COPY parse_service ./parse_service` 가 `tests/fixtures/` 를 포함 |
| F16 | 번들 빌드가 가드를 **강제 실행**한다 | `build-bundle.sh:145-155` — `SKIP_VERIFY=1` 이 아니면 `verify-bundle.sh --images`/`--imports` 를 돌리고 실패 시 `exit 1`(번들 미생성) |
| F18 | 성공 경로 `gate_summary` 에는 **`error` 키가 없다** | `excel_gate.py:339` `return {"ok": all(...), "sheets": sheets_out}` — `error` 는 F10 의 대체 dict 에만 있다 |
| F19 | `build-bundle.sh:122-125` 는 **용량 문구가 아니다** | 2026-08-07 스테이징 미삭제 사고 기록(옛 이미지가 다음 번들에 딸려 들어간 건). **건드리지 않는다** |
| F17 | amd64 컨테이너에서 soffice 변환이 **성공**한다 | probe 이미지에서 `--convert-to xlsx` → `filter : Calc Office Open XML`, exit=0 (`javaldx` 경고는 무해) |

## 2. 설계 — 변환 지점은 **레인 입구 한 곳**

`kordoc_backend.py:836` 을 땜질하지 않는다. 거기만 고치면 F4(전결 `.xls` 가 kordoc 으로
새는 조용한 품질 손실)가 남고 `gate/excel_gate.py:176,178` 에서 같은 결함을 또 고쳐야 한다.

`parsers/excel/__init__.py` 의 csv 분기(F8) **바로 다음**에서 `.xls` 바이트를 `.xlsx`
바이트로 바꾼다. 그러면 `:69` 의 `suffix` 가 `.xlsx` 가 되어:

- `auto_backend` 의 전결 Tier1·계층 Tier1.5 확장자 게이트를 **통과**(F4 해결)
- kordoc 백엔드의 동반 워크북 읽기가 **성공**(F2 해결)
- `compute_gate_summary` 가 여는 것도 변환본이라 **게이트가 산다**(§2.4 의 검증 대상)
- 하류(백엔드 3종·게이트·청킹) **코드 변경 0**

### 2.1 판별은 **매직바이트**로 — 확장자 100% 판정 금지

확장자만 보면 두 방향으로 깨진다:
- **이름만 `.xls` 인 xlsx(zip)** — 현재는 openpyxl 이 zip 을 스니핑해 **정상 처리된다.**
  무조건 변환하면 soffice 없는 환경에서 **되던 문서가 죽는다**(= "무영향" 주장 붕괴)
- **이름이 `.xlsx` 인 진짜 BIFF** — 지금도 죽고, 확장자 기준 변환으로는 안 고쳐진다

```python
# parsers/excel/__init__.py — csv 분기(:63-68) 바로 다음
_CFB_MAGIC = b"\xd0\xcf\x11\xe0"    # OLE Compound File — 레거시 .xls 의 컨테이너

is_biff = file_bytes[:4] == _CFB_MAGIC
if is_biff:
    from parse_service.parsers.excel.xls_to_xlsx import xls_bytes_to_xlsx
    file_bytes = xls_bytes_to_xlsx(file_bytes, safe_filename)

# ★ 강제 .xlsx 는 **바이트를 실제로 갈아끼운 경우에만**.
#   v2 초안은 zip 매직이면 무조건 .xlsx 로 했는데, .xlsm 도 OOXML=zip 이라
#   매크로 워크북의 임시파일 suffix 가 조용히 .xlsx 로 바뀐다(이득 0 의 동작 변경,
#   kordoc CLI 는 확장자로 디스패치한다). tests 에 .xlsm 케이스가 0건이라
#   회귀로도 안 잡힌다 → 아래처럼 원 확장자를 보존한다.
suffix = ".xlsx" if (is_csv or is_biff) else (Path(safe_filename).suffix.lower() or ".xlsx")
```

즉 **현행 `:69` 식에 `is_biff` 만 더한다**(csv 선례와 같은 모양). 변환하지 않은 입력의
확장자는 **하나도 바뀌지 않는다**:
- `.xlsm` → `.xlsm` 유지 (v2 초안의 회귀 제거)
- 이름만 `.xls` 인 zip → `.xls` 유지 = **오늘과 완전히 동일한 kordoc 경로**
  (오늘도 `_OPENPYXL_SUFFIXES` 불일치로 kordoc 행이고, openpyxl 이 zip 을 스니핑해 성공한다)

> 두 렌즈가 서로 다른 식을 제안했다. `ext in {".xlsx",".xlsm"} else ".xlsx"` 형태는
> 이름만 `.xls` 인 zip 의 suffix 를 `.xlsx` 로 **바꿔** 라우팅(Tier1/1.5)까지 달라진다.
> 더 "올바른" 처리일 수는 있으나 이번 목표(= `.xls` 개통) 밖의 동작 변경이라 채택하지
> 않는다. 필요해지면 별건으로 다룬다(`deferred.md`).

`document_title` 은 `Path(safe_filename).stem`(`:77`) — **확장자 없는 stem** 이라 확장자
교체가 제목에 영향을 주지 않는다(csv 와 동일 관례).

**변환은 반드시 `tempfile`/게이트 `try` 밖, csv 분기 직후에서** 수행한다. 게이트 `try`
안으로 들어가면 변환 실패가 F10 경로로 삼켜져 **조용히 `ok=False` 적재 판정으로 degrade**
한다(이 plan 이 금지하는 실패 모드).

### 2.2 `xls_bytes_to_xlsx` — 바이트 어댑터 (신규 `parsers/excel/xls_to_xlsx.py`)

`csv_to_xlsx.py` 와 같은 층위·명명. 기존 `loaders/xls_converter.convert_xls_to_xlsx` 를
재사용하되 **`output_dir` 을 명시 전달**한다 — 미지정이면 변환기가 자기 mkdtemp 를 만들고
정리하지 않아(F13) **문서 1건당 xlsx 사본이 `/tmp` 에 영구 누적**된다(gunicorn 워커는
장수 프로세스, `/tmp` 는 `KORDOC_MD_OUT` 과 공유).

```python
import shutil, tempfile
from pathlib import Path
# ★ 모듈 최상위 import — 함수 내부 지연 import 면 V2b 의
#   setattr(xls_to_xlsx, "convert_xls_to_xlsx", fake) 패치 지점이 존재하지 않는다
from parse_service.parsers.excel.excel_parser_rag.loaders.xls_converter import convert_xls_to_xlsx


def xls_bytes_to_xlsx(file_bytes: bytes, _filename: str | None = None) -> bytes:
    tmpdir = Path(tempfile.mkdtemp(prefix="xls2xlsx_"))
    try:
        src = tmpdir / "src.xls"          # ★ 내부 고정 이름 — 사용자 파일명을 쓰지 않는다
        src.write_bytes(file_bytes)
        out = convert_xls_to_xlsx(src, output_dir=tmpdir)   # → tmpdir/"src.xlsx"
        return out.read_bytes()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
```

**내부 이름을 고정하는 이유** — 변환기는 산출물을 `out_dir / f"{src.stem}.xlsx"` 라는
**정확한 이름으로만** 찾는다(`xls_converter.py:97`). soffice 는 경로를 URL 로 다루므로
`#`·`%` 등이 든 실무 한글 파일명에서 산출 basename 이 어긋나면 **exit=0 인데
`converted.is_file()` 이 거짓** → `XlsConversionError`. 픽스처는 전부 깨끗한 ASCII 라
V1~V7 어디서도 재현되지 않고 **현장에서만** 터진다. 어댑터가 사용자 파일명을 디스크
이름으로 쓸 이유도 없다 — `document_title` 은 `__init__.py:77` 에서, 백엔드 임시파일은
`excel_parser_` prefix 로 각각 독립적으로 잡힌다.

보강: 변환기의 산출물 탐색에 **"정확한 이름이 없으면 `out_dir` 내 유일한 `*.xlsx`"**
폴백을 넣는다(§2.3-4 의 listdir 진단과 같은 자리) — 이름 어긋남 전반이 닫힌다.

실패 시 `XlsConversionError` 를 그대로 올린다 → F11 이 `ParserError` 로 감싸 **원인
문자열이 보존**된다(조용한 폴백 없음). `finally` 의 rmtree 가 **예외 경로에서도** 도는
것이 이 설계의 실제 축이다(§4 V2b).

**tmpdir 소유권 — 두 모듈이 서로의 디렉터리를 지우지 않는다.**

| 디렉터리 | 만드는 곳 | 지우는 곳 | 용도 |
|---|---|---|---|
| `xls2xlsx_*` | 어댑터(§2.2) | 어댑터 `finally` | 입력 `.xls` + 산출 `.xlsx` |
| `<gettempdir()>/lo_*` | 변환기(§2.3) | 변환기 `finally` | soffice 프로필 + `HOME` |

★ 프로필은 **`gettempdir()` 직하**에 만든다(`tempfile.mkdtemp(prefix="lo_")`). `output_dir`
**안에 중첩시키지 않는다** — 두 가지 이유다: ① V3·V6 의 `lo_*` 잔존 assert 가 최상위를
세므로 중첩하면 **항상 0 == 0 인 vacuous assert** 가 되어 R3 을 아무도 관측하지 못한다,
② `output_dir` 안에 LibreOffice 프로필 트리가 생기면 §2.2 의 "유일한 `*.xlsx`" 폴백 탐색에
프로필 파일이 섞인다(폴백은 **비재귀 glob** 으로 한정한다).

변환기는 **자기 프로필 디렉터리만** 소유·삭제하고 `output_dir` 은 **절대 건드리지 않는다**.
(변환기가 `output_dir` 기준으로 rmtree 하면 반환 직전에 산출물이 지워져
`out.read_bytes()` 가 `FileNotFoundError` 로 죽는다 — V3 첫 실행에서 즉시 드러난다.)

**누수 원형(F13)의 다른 호출처** — `loaders/workbook_loader.py:39` 은 여전히
`convert_xls_to_xlsx(src)` 를 **`output_dir` 없이** 부른다. 레인 입구 변환 이후 이 분기는
`.xls` 가 도달하지 않아 dead 가 되지만, **dead 를 근거로 남겨두지 않는다** — 같은 함수의
두 호출 규약이 다르면 다음 사람이 또 밟는다. 이 호출에도 `output_dir` 을 명시하고
(`tempfile.mkdtemp` 를 호출자가 만들어 넘김), 변환기 docstring 의 "호출자가 정리 책임
없음" 문구를 정정한다(§3 표에 포함).

### 2.3 `xls_converter` — 프로필·고아 프로세스 수명 재설계

현행 `subprocess.run(timeout=180)` 은 `TimeoutExpired` 에서 **직계 자식만** 죽인다.
soffice 런처가 띄운 `soffice.bin` 이 고아로 남아 프로필의 `.~lock` 을 쥐면, 같은 워커의
다음 요청이 같은 잠긴 프로필을 재사용해 **그 워커의 `.xls` 변환이 영구 실패**한다
(gunicorn `--timeout 3600`, 워커 회수 없음). 따라서:

1. **변환 1회당 독립 프로필** — `profile = tempfile.mkdtemp(prefix="lo_")` (= `gettempdir()`
   **직하**, §2.2 의 이유), `-env:UserInstallation=file://{profile}`, `finally` 에서
   **그 디렉터리만** rmtree(`output_dir` 은 건드리지 않는다). per-pid 로 하면 타임아웃
   오염이 영구화된다. 변환마다 독립이므로 F14 의 동시성 논의도 자연 해소된다
2. 인자 순서는 **실행파일 직후 고정** — `[soffice, "-env:UserInstallation=…", "--headless", "--norestore", …]`
3. `Popen(start_new_session=True)` + `TimeoutExpired` 시 `os.killpg(pgid, SIGKILL)` 후 잔여 대기
4. `--convert-to` 필터 고정 — `"xlsx:Calc MS Excel 2007 XML"`. 실패 메시지에
   `os.listdir(out_dir)` 를 덧붙여 "exit=0 인데 산출물 없음" 을 진단 가능하게 한다
5. **변환 전용 타임아웃 60s** (기존 180s 에서 하향, 근거는 §6 R2)
6. `HOME` 은 **전역 ENV 로 바꾸지 않는다** — 이 이미지는 root(HOME=/root 쓰기 가능)이고
   compose 에 `read_only`/`user` 설정이 없어 전제가 성립하지 않는다(F17 로 실증). 전역 ENV 는
   npm/kordoc 캐시·java 임시경로 등 무관한 소비자까지 바꾼다. 대신 **변환 subprocess 에만**
   `env={**os.environ, "HOME": str(tmpdir)}` 를 넘긴다
7. 변환기 docstring 의 "호출자가 정리 책임 없음" 문구를 정정한다(F13 이 오해의 근원)

### 2.4 게이트 판정 뒤집힘 — 이번 설계의 최대 미검증 축

게이트는 `ERROR_RE`(`#REF!`/`#NAME?` 등)를 **캐시값 워크북과 수식 워크북 양쪽**에서
스캔한다(`excel_gate.py:176,178`). LibreOffice 왕복은 값을 **재계산**하므로 두 방향으로
판정을 뒤집을 수 있다:
- **오검(위험)**: 원본의 `#REF!` 가 재계산으로 지워져 **불량 문서가 통과**
- **오탐**: LO 미지원 함수가 `#NAME?` 가 되어 **정상 문서가 차단**

"게이트가 산다"는 주장은 이 축을 검증해야 성립한다 → V3 에서 양방향 assert.
재계산이 실제로 문제로 확인되면 §2.3 에 recalc 억제 옵션을 추가한다.

**픽스처는 반드시 "간접 참조" 모양이어야 한다** — 이게 이 축의 전부다.

게이트는 캐시값 워크북과 수식 워크북을 **하나의 `ref_set` 으로 합치고**(`excel_gate.py:190-196`),
`ERROR_RE`(`:25`)는 셀 문자열 어디든 매치한다. 따라서 **수식 텍스트에 `#REF!` 토큰이 있는**
통상 형태(`=#REF!A1`, `=SUM(#REF!)`)를 픽스처로 쓰면, LibreOffice 가 캐시값을 재계산으로
지우든 말든 수식 스캔이 **무조건** 잡는다 → V3 ① 은 **실패할 수 있는 조건이 없다**
(= 오검 축이 무검증인 채 초록불).

그래서 `excel_gate.py:200-204` 주석이 명시한 케이스를 그대로 쓴다:

| 셀 | 내용 | 성격 |
|---|---|---|
| `J9` | `=#REF!` | 오류의 출처 |
| `H3` | `=J9` | **수식 텍스트에 에러 토큰이 없고, 캐시값만 `#REF!`** |

V3 ① 은 **`H3` 좌표가 ref_error cells 에 포함되는지**를 assert 한다. LibreOffice 왕복이
재계산으로 캐시값을 지우면 `H3` 가 빠져 **실패한다** — 실패 조건이 실재한다.

**픽스처 전제 assert** — v5 까지 적어둔 "`load_workbook(fx, data_only=True)` 로 캐시값
`#REF!` 실재 확인"은 **실행 불가능한 형태**였다: 대상이 `.xls` 면 openpyxl 이 못 읽고(그게
F1 그 자체), openpyxl 로 만든 `.xlsx` 면 캐시값이 애초에 없어 **항상 실패**한다. 대신
`make_xls_fixtures.py` 안에서 생성된 `.xls` 를 **soffice 로 1회 `.xlsx` 로 되돌린 뒤**
두 조건을 모두 assert 한다(하나라도 깨지면 픽스처 생성 실패):
- (a) `H3` 의 **캐시값**(`data_only=True`)이 `#REF!` 이다
- (b) `H3` 의 **수식 텍스트**(`data_only=False`)에 `#REF!` 가 **없다**

(b) 가 없으면 위 vacuity 가 그대로 되살아난다 — 캐시값 유무만 보는 검사로는 못 막는다.

**오탐 축(②)의 한계** — `SUM` 만으로는 `#NAME?` 가 날 리 없어 ② 도 실패 불가다. ② 는
**"오탐 축 검증"이 아니라 스모크**임을 명시하거나, LO 지원 폭이 다른 함수를 1개 넣는다.

**한계** — 우리 픽스처는 LibreOffice 가 쓴 `.xls` 를 LibreOffice 로 다시 여는
**LO→LO 왕복**이라, 실제 현장의 "Excel 이 쓴 캐시값" 시나리오와 완전히 같지 않다.
현장 `.xls` 로 한 번 더 확인하는 것이 최종 근거다(§구현 후 검증).

### 2.5 컨테이너에 LibreOffice 설치

`Dockerfile.parse-svc` 의 apt 레이어에 `libreoffice-calc libreoffice-core` 추가.
`fonts-nanum` 은 렌더링용이라 xls→xlsx 변환에 불필요할 수 있다 → **V6 에서 빼고도 되는지
확인하고, 되면 제거해 증분을 줄인다.**

### 2.6 폐쇄망 (CLAUDE.md 폐쇄망 규칙)

- **env 신설/삭제/기본값 변경 없음** → `.env*` 5종·compose 2종 변경 없음(전역 ENV 도 §2.3-6 으로 회피)
- **번들 산출물**: parse-svc 이미지가 커진다 → §5 수치는 **`_workspace/03-dev-progress.md`**
  에 반영한다. ⚠️ `scripts/airgap/build-bundle.sh:122-126` 은 **건드리지 않는다** — 용량
  문구가 아니라 2026-08-07 스테이징 미삭제 사고 기록이다(F19). v2 이전 지시가 여기 남아
  §3·F19 와 모순이었다(v4 정정). `docs/airgap-deploy.md:98` 은 "2GB 초과 시 분할" 문구라
  증분(+8%)으로 바뀌는 서술이 없다 → **대상에서 뺀다**
- **가드**: `verify-bundle.sh` 엑셀 스모크에 `.xls` 케이스 추가(§4 V5). `build-bundle.sh:149-152`
  가 이를 강제 실행하므로(F16) 가드를 만들고 안 돌리는 실수는 구조적으로 막힌다
- **탈출구**: soffice 부재 시 기존 `XlsConversionError` 메시지가 그대로 노출된다(§4 V2)

## 3. 변경 파일

| 파일 | 변경 |
|---|---|
| `parse_service/parsers/excel/xls_to_xlsx.py` | **신규** — 바이트 어댑터(§2.2) |
| `parse_service/parsers/excel/__init__.py` | 매직바이트 판정 + 변환 분기 + `suffix` 반영(§2.1) |
| `parse_service/parsers/excel/excel_parser_rag/loaders/xls_converter.py` | 최상위 `lo_*` 프로필·killpg·필터 고정·타임아웃 60s(주입 가능하게)·`env` HOME·**산출물 탐색 폴백(비재귀 glob)**·docstring 정정(§2.2/§2.3) |
| `parse_service/parsers/excel/excel_parser_rag/loaders/workbook_loader.py` | `:39` 의 `convert_xls_to_xlsx(src)` 에 `output_dir` 명시(누수 원형 제거, §2.2) |
| `parse_service/parsers/excel/excel_parser_rag/backends/auto_backend.py` | `:29-31` 주석 정정 — "소속 backend 가 soffice 변환을 내부 처리"는 사실이 아니었다 → "레인 입구(§2.1)에서 변환되어 `.xls` 가 여기 도달하지 않는다" |
| `Dockerfile.parse-svc` | `libreoffice-calc libreoffice-core` 설치(§2.5) |
| `scripts/airgap/verify-bundle.sh` | `.xls` 왕복 스모크 + `soffice` 존재 확인(§4 V5). 기존 `XLS_PY`(실제로는 xlsx 스모크)와 **이름 충돌 금지** → 새 상수는 `BIFF_PY` |
| ~~`docs/airgap-deploy.md`~~ | **대상에서 제외**(v4) — `:98` 은 "2GB 초과 시 분할" 문구라 증분(+8%)으로 바뀌는 서술이 없다. 용량 반영처는 `_workspace/03-dev-progress.md` 하나다. ⚠️ `build-bundle.sh:122-126` 도 **건드리지 않는다**(F19) |
| `parse_service/tests/fixtures/make_xls_fixtures.py` | **신규** — 픽스처 3종의 `.xlsx` 원본을 openpyxl 로 만들고 soffice 로 `.xls` 변환하는 **재현 스크립트**. 산출물뿐 아니라 생성 경로를 커밋해 검토·재생성이 가능하게 한다 |
| `parse_service/tests/fixtures/legacy_sample.xls` | **신규** — 값·병합셀 포함 |
| `parse_service/tests/fixtures/delegation_sample.xls` | **신규** — "전결" 키워드 포함(F4 회귀용) |
| `parse_service/tests/fixtures/broken_formula.xls` | **신규** — `#REF!`(캐시값으로 실재) + 정상 수식(SUM) 동시 포함(§2.4) |
| `parse_service/tests/test_parser_excel_xls.py` | **신규** — §4 V1~V4 |
| `_workspace/01-architecture.md`, `_workspace/02-changes.md`, `_workspace/03-dev-progress.md` | 엑셀 레인 입구 변환 규칙·경위·용량 반영 |
| `deferred.md` | §0 비목표 4건을 근거·재검토 트리거와 함께 이관 |

## 4. 검증 — 전부 실행하고 출력으로 증명한다

> v1 의 V1~V4 는 **"실행 불가 아니면 항상 통과"** 였다(F9·F10·F12). 아래는 관측 가능한
> 심볼만 쓴다. 각 항목에 **무엇을 읽어 assert 하는지**를 명시한다.

- **V1 라우팅 (단위, soffice 불필요 — soffice 없는 CI 의 유일한 회귀선)**
  - `monkeypatch.setattr("parse_service.parsers.excel.xls_to_xlsx.xls_bytes_to_xlsx", fake)`
    ← **지연 import 대상 모듈의 속성**에 건다(`parsers.excel` 네임스페이스에 걸면 무효)
  - `fake` 는 호출 카운터를 올린다 → **호출 0 이면 실패**
  - 하류는 `test_parser_excel.py` 의 CapturingBackend 패턴으로 스텁 → `input_path.suffix` 관측
  - 케이스: ① 이름 `.xls` + **CFB** → 호출 1, suffix `.xlsx`
    ② 이름 `.xlsx` + CFB → 호출 1, suffix `.xlsx`
    ③ 이름 `.xls` + **zip** → **호출 0**, suffix `.xls`(현행 보존)
    ④ 이름 `.xlsm` + zip → **호출 0**, suffix **`.xlsm` 보존**(v2 회귀 방지)
    ⑤ 이름 `.xls` + 임의 텍스트 바이트 → **호출 0**, suffix `.xls`
    (하류를 스텁하므로 "실패"까지는 관측 불가 — 관측 가능한 것만 assert 한다)
  - ⚠️ **여기에 tmpdir 잔존 assert 를 두지 않는다** — `xls_bytes_to_xlsx` 자체가 fake 로
    대체돼 tmpdir 이 아예 안 생기므로 **무조건 통과**한다(v1 #3 재발 지점). → V2b 로 분리
- **V2 탈출구 (단위)** — `monkeypatch.setattr(xls_converter, "find_soffice", lambda: None)`
  → `pytest.raises(ParserError, match="soffice")`. 조용한 폴백이 아님을 못박는다
- **V2b 임시물 누수 (단위, soffice 불필요 — 래퍼 본체를 실제로 태운다)**
  - `parse_service.parsers.excel.xls_to_xlsx.convert_xls_to_xlsx` **만** 스텁한다
    (받은 `output_dir` 에 유효 xlsx 를 써주는 fake) → `xls_bytes_to_xlsx` 본체는 진짜로 실행
  - 관측 경로는 `/tmp` 하드코딩이 아니라 **`Path(tempfile.gettempdir())`**
    (macOS 호스트 dev 의 `TMPDIR` 은 `/var/folders/...` 라 `/tmp/…` glob 은 항상 0개다)
  - 관측 접두사는 **`xls2xlsx_*` 하나만**. `excel_parser_rag_xls_*`·`lo_*` 는 **진짜 변환기만**
    만들므로(스텁 상태에선 애초에 생성 안 됨) 여기서 세면 **또 vacuous assert** 가 된다
    (v1 #3 → v2 #2 에 이은 3번째 재발 지점이었다). 그 둘은 V3·V6 로 옮긴다
  - **예외 경로 1건**: fake 가 `XlsConversionError` 를 던져도 잔존 0(`finally` rmtree 가 실제 축)
- **V3 왕복·충실도·게이트 (통합, `skipif` = soffice 부재 **또는** `KORDOC_BIN` 부재)**
  - `parse(Path(fx).read_bytes(), "legacy_sample.xls")` → `chunks >= 1`
  - **값**: 특정 셀 문자열이 청크 본문에 존재
  - **병합**: 같은 V3 가 이미 뽑는 kordoc 산출물에서 **`colspan` 개수**(또는 병합 좌표
    문자열 `A5:C5`)가 원본과 일치. ← "청크/게이트 신호로 관측됨" 같은 모호한 표현을 쓰면
    구현자가 **항상 참인 assert** 를 고를 수 있다(§4 서두 규칙 위반이었다)
  - **게이트**(F10 우회): `gate_summary.get("error") is None` **and** `gate_summary["ok"] is True`
    **and** `gate_summary["sheets"]` 비어있지 않음(가능하면 시트 수 == 원본 시트 수)
    ← 성공 경로엔 `error` 키가 **없다**(F18). `[...]` 로 읽으면 KeyError 다
  - **§2.4 오검 축(①)**: `broken_formula.xls` → **`H3` 좌표가 ref_error cells 에 포함**
    (간접 참조라 재계산으로 캐시값이 지워지면 빠진다 = 실패 조건 실재). 수식 텍스트에
    에러 토큰이 있는 셀로 대체하면 이 assert 는 무의미해진다 — §2.4 표대로 만들 것
  - **오탐 축(②)**: 정상 수식 시트에서 ref_error 가 새로 생기지 않는가.
    ⚠️ `SUM` 만으로는 실패 불가라 **이건 스모크**다(§2.4 한계)
  - **실 변환기 프로필 잔존 0**(V2b 에서 이관): `Path(tempfile.gettempdir())` **직하**의
    `lo_*` 개수가 호출 전후 동일. (§2.3-1 이 프로필을 최상위에 만들기 때문에 이 glob 이
    실제로 세는 대상이 된다 — 중첩 생성으로 바뀌면 이 assert 는 그 순간 무의미해진다)
  - `excel_parser_rag_xls_*` 는 **개수 비교가 아니라 "하나도 생기지 않는다"** 로 적는다.
    §2.2 이후 `output_dir=None` 호출처가 남지 않으므로(어댑터·`workbook_loader.py:39` 둘 다
    명시 전달) 이 접두사는 애초에 생성되지 않아야 한다 = **이 실행에서 도달한 호출처가
    규약을 지켰다**는 증명(V3 는 레인 경로만 태우므로 `workbook_loader.py:39` 분기까지
    증명하지는 못한다 — 문구를 과장하지 않는다). 절대 존재검사 대신 **호출 전후 집합 차분**
    으로 쓴다(예전 실행이 남긴 stale 디렉터리가 무관하게 실패시키는 것을 막는다)
  - **kordoc 열화 없음**(F3 대비): `kordoc(원본 .xls)` 와 `kordoc(변환 .xlsx)` 의
    `<table>`·`colspan` 개수 비교 — §0 의 "`.xlsx` 와 동등" 주장에 실제 근거를 준다
  - 변환 소요(초)를 기록해 §6 R2 의 근거로 남긴다
- **V4 전결 회귀 (F4)** — F9·F12 때문에 두 층으로 나눈다.
  **skipif**: ① soffice 부재, ② soffice **또는** `KORDOC_BIN` 부재(auto 가 kordoc 으로
  떨어질 수 있다). V3 에만 skipif 를 적어 둔 v2 의 누락을 메운다
  - ① **백엔드층**: `xls_bytes_to_xlsx` 로 변환한 바이트를 임시 `.xlsx` 로 쓰고
    `get_backend("auto").parse(path, cfg)` 직접 호출 →
    `stats["routed_backend"] == "openpyxl"` **and** `chunk_type == "delegation_rule"` ≥ 1
  - ② **레인층**: `parse(Path(fx).read_bytes(), "delegation_sample.xls")` 결과 청크 `text` 에
    전결 규칙 문자열이 포함되는지(레인 밖에서는 `chunk_type` 을 볼 수 없다 — F12)
- **V5 폐쇄망 가드** — `verify-bundle.sh` 에 `.xls` 스모크 추가 후 **실제 실행**
  - 입력: 이미지 안의 `parse_service/tests/fixtures/legacy_sample.xls`(F15 로 존재 보장)
  - **픽스처 부재 시 스킵이 아니라 실패**(조용한 통과 금지)
  - `command -v soffice` 실패와 왕복 실패를 **별도 메시지**로 분리
  - `bash scripts/airgap/verify-bundle.sh --imports` 출력으로 통과 증명
- **V6 이미지 (`--platform linux/amd64` 필수)** — 번들은 QEMU 크로스빌드 amd64 다.
  arm64 로만 검증하면 실제 반입 대상과 §5 수치가 어긋난다
  - 새 `Dockerfile.parse-svc` 를 amd64 로 빌드 → 컨테이너 안 soffice 왕복 1회 성공
  - **타임아웃 강제 종료 후 다음 변환이 정상 성공**(§2.3 고아 프로세스 회귀) —
    그리고 **정상 변환 1회 / 타임아웃 강제 종료 1회 각각**에 대해 `tempfile.gettempdir()`
    **직하** `lo_*` 개수가 전후 동일(R3 이 방어하는 프로필 잔존을 여기서만 실제로 관측한다)
  - **타임아웃을 어떻게 유발하나**: `_CONVERT_TIMEOUT_SEC` 는 모듈 상수라 `docker run python -c`
    스모크에서 바꾸기 어렵다 → **모듈 속성 monkeypatch(1s)** 또는 함수 인자로 주입한다.
    새 env 를 만들지 않는다(§2.6 "env 신설 없음" 유지). 이 수단을 안 정해두면 구현자가
    이 회귀선을 "재현 불가"로 조용히 건너뛴다
  - `fonts-nanum` 없이도 변환되는지 확인(§2.5)
  - 같은 이미지로 §5 수치 재확인
- **V7 회귀** — `pytest parse_service/tests -q` 전체 통과(csv·xlsx 기존 경로 무영향)

## 5. 크기 실측 (2026-08-13 완료, `--platform linux/amd64` 실빌드)

| 항목 | 비압축 | 압축(`docker save \| gzip -1`) |
|---|---|---|
| base `python:3.12-slim` | 179 MB | 43.3 MB |
| + `libreoffice-calc libreoffice-core fonts-nanum` | 817 MB | 219.9 MB |
| **증분** | **+638 MB** | **+176.6 MB** |

번들 영향: `kbp-parse-bundle-amd64.tar.gz` 2.18 GB → **약 2.36 GB (+8%)**,
`kbp-airgap-bundle` 3.13 GB → 약 3.31 GB. (gzip -1 기준의 보수적 수치)

컨테이너 동작(F17): `--convert-to xlsx` → `filter : Calc Office Open XML`, exit=0.
`javaldx` 경고가 뜨지만 Calc 필터는 JVM 불필요 — 실제 이미지엔 openjdk-21 이 있어 V6 에서 재확인.

## 6. 리스크

- **R1 번들 비대** — 압축 +176.6 MB(+8%). 사용자가 A 안을 선택함(2026-08-13). 대안 xlrd 는
  수식 텍스트 유실로 게이트 `#REF!` 스캔이 `.xls` 에서 무력화되어 탈락
- **R2 변환 지연·워커 점유** — 최악 동시 점유 = **워커수 × 변환 타임아웃**. `-w 4` × 180s
  이면 손상 `.xls` 4건으로 parse-svc 전체(pdf/docx 포함)가 3분 무응답. 그래서
  **변환 타임아웃을 60s 로** 낮춘다(§2.3-5) → 최악 4건 × 60s.
  **유입 경로가 `.xls` 만이 아니다** — CFB 매직은 BIFF 전용이 아니라서 구 `.doc`/`.ppt`,
  **암호화된 `.xlsx`** 도 변환을 타고 타임아웃까지 워커를 점유한 뒤 실패한다(오늘은
  openpyxl 이 즉시 실패한다). 정밀 판별은 비목표로 이관했다.
  타임아웃 60s 의 근거는 **호스트 실측이 아니라 V6 의 amd64 컨테이너 실측**으로 삼는다
- **R3 프로필 오염** — 타임아웃 시 soffice.bin 고아가 `.~lock` 을 쥐어 워커가 영구 불능 →
  §2.3 의 변환 1회당 최상위 `lo_*` 프로필 + killpg 로 방어, V6 에서 회귀 확인
- **R4 변환 충실도·게이트 뒤집힘** — §2.4. V3 이 값·병합·양방향 ref_error 로 검증
- **R5 빌드 시간** — QEMU amd64 위에서 libreoffice apt 설치는 느리다(실측 63s, 단 apt 캐시
  상태에 따라 변동). `build-all-bundles.sh` 의 3회 재시도 예산에 영향

## 7. 구현 후 검증 — 계획서에서 더 다투지 않고 **실행으로 닫는다**

### 실행 결과 (2026-08-13 구현 완료 시점)

| # | 결과 |
|---|---|
| P1 | ✅ **닫힘 — 필터를 고정하지 않았다.** F17/§5 가 실증한 무필터 `--convert-to xlsx` 를 그대로 쓴다. 고정하려던 `"xlsx:Calc MS Excel 2007 XML"` 은 실측된 적 없는 이름이라 채택하지 않았다(대신 산출물 탐색 폴백 + `listdir` 진단으로 이름 어긋남을 덮는다) |
| P2 | ✅ `Popen(stdout/stderr=PIPE, start_new_session=True)` + `communicate(timeout)` + `killpg(SIGKILL)` 로 구현. 컨테이너에서 타임아웃 강제 종료 실측 — 교착 없이 `XlsConversionError` |
| P3 | ✅ tmpdir 소유권 분리 정상 — 산출물 `read_bytes()` 성공(변환기는 프로필만 지운다) |
| P4 | ✅ `BIFF_CHECK_TIMEOUT` 기본 **300s** 로 설정. amd64(QEMU) 컨테이너 실측 **콜드 18.1s / 웜 8.7s** — 300s 예산 충분, 변환 타임아웃 60s 도 타당 |
| P5 | ✅ **닫힘(기우였다)** — doc_guard(:8001) 실측: `.xls` 를 정상 판정한다(`result=fail`, `ref_error 참조오류 시트 H3, J9` / `unclear_header`). `SUPPORTED_EXTENSIONS` 는 **호출부 0건**인 multipart `/v1/check` 전용이고, 실제 게이트는 `gate_summary` 를 받는 `/v1/check-excel` 이라 확장자와 무관하다 |
| P6 | ✅ **닫힘** — 사용자가 현장 `.xls`(239KB, 진짜 BIFF) 제공. 레인 통과: **21청크, 게이트 ok=True(2시트, findings 0)**, 내용 보존 확인(29행×257열 대장, 병합 3). ⚠️ 이때 **변환만 38s** 로 측정돼 타임아웃 60s 는 여유 1.6배뿐 → **120s 로 상향 + `KBP_XLS_CONVERT_TIMEOUT` env 신설**(아래 "계획 이탈") |
| P7 | ✅ 좌표 정정 반영(§1) |
| P8 | ✅ 오탐 축을 **별도 시트**(`정상`)로 분리 — 실측에서 `{"sheet":"정상","ok":true,"findings":[]}` 확인 |
| P9 | ✅ 임시물 assert 를 전후 **집합 차분**으로 통일 |
| P10 | ✅ V1 에서 `compute_gate_summary` 도 스텁 |

**추가 발견(계획에 없던 것)** — LibreOffice 는 캐시 오류값을 **소문자**로 쓴다(`#REF!`→`#ref!`,
수식은 `="#ref!"`). 게이트 `ERROR_RE` 가 대문자 전용이라 `.xls` 문서의 참조오류 검사가
**통째로 침묵**했다 → `re.IGNORECASE` 추가. §2.4 의 픽스처 전제 assert 가 발화해 잡혔다.

**계획 이탈 1건 — env 를 신설했다(§2.6 은 "env 신설 없음" 이었다).**
현장 `.xls` 실측에서 **변환만 38s**(호스트 맥) 가 나왔다. 타임아웃 60s 는 여유가 1.6배뿐이라
**폐쇄망 서버가 느리면 정상 문서가 실패**한다. 근거가 바뀌었으므로 기본값을 **120s** 로 올리고
`KBP_XLS_CONVERT_TIMEOUT` 으로 현장에서 조절할 수 있게 열었다(탈출구 원칙).
CLAUDE.md 규칙대로 **선언처 5곳 전수 갱신**: `.env.example`, `.env.airgap.example`,
`.env.parse-only.example`, `docker-compose.yml`, `docker-compose.airgap.yml`.
dev·airgap `compose config` 양쪽 보간 확인(`KBP_XLS_CONVERT_TIMEOUT: "120"`).

**D56(전결 `.xls`)도 실측으로 닫혔다** — 실제 위임전결 문서 `.xlsx`(72KB)를 `.xls`(120KB)로
변환해 비교: **양쪽 다 `routed_backend=openpyxl`, `delegation_rule` 207개, 본문 동일**.
변환 전이라면 확장자 게이트에 막혀 **207 → 0** 이 됐을 자리다.



아래는 "한 번 돌리면 즉시 드러나는" 종류다(2라운드 종합의 분류). 착수 후 **실측 로그·테스트
출력을 증거로** 닫고, 완료 판정에 포함한다.

| # | 항목 | 어떻게 닫나 |
|---|---|---|
| P1 | `--convert-to` 필터명 `"xlsx:Calc MS Excel 2007 XML"` 이 실제로 수용되는가 | §5/F17 실측은 **무필터** 성공이다. V6 에서 그 필터명으로 1회 왕복 → 거부되면 F17 이 실증한 무필터 형태로 되돌린다 |
| P2 | `Popen` 출력 수집 교착 | `with Popen(..., stdout=PIPE, stderr=PIPE, text=True, start_new_session=True) as p: p.communicate(timeout=…)`. `wait()` 를 쓰면 soffice 의 `javaldx` 경고로 파이프가 찬다. TimeoutExpired 시 `os.killpg(os.getpgid(p.pid), SIGKILL)` 후 `communicate()` 재호출로 잔여 출력 회수 |
| P3 | tmpdir 소유권 오구현 | V3 첫 실행에서 `out.read_bytes()` FileNotFoundError 로 즉시 드러남 |
| P4 | 폐쇄망 스모크 타임아웃 예산 | 기존 `IMPORTS_CHECK_TIMEOUT:-120` 안에서 QEMU amd64 + LO 콜드스타트 + 신규 프로필 생성이 끝나는가. V6 에서 **첫 변환/두 번째 변환 소요를 각각 기록**해 `BIFF_CHECK_TIMEOUT`(기본값 후보 300)을 정한다. 부족하면 **번들 생성 자체가 막힌다**(F16) |
| P5 | doc_guard 실측 | 변환 성공한 `.xls` 를 `POST /v1/check-excel` 에 `filename="legacy_sample.xls"` 로 넣어 (a) 정상 판정인지 (b) unsupported 스킵인지 curl 1회 확인. **스킵이면** `deferred` 재검토 트리거를 "신호 degrade" 가 아니라 **"게이트 미판정 = 무검문 적재"** 로 격상한다 |
| P6 | 현장 `.xls` 로 §2.4 재확인 | 우리 픽스처는 LO→LO 왕복이라 "Excel 이 쓴 캐시값" 과 다르다. 사용자가 폐쇄망에서 겪은 실제 파일로 게이트 판정을 한 번 더 본다. **파일을 못 구하면 조용히 넘어가지 말고 완료 보고에 "미검증" 으로 명시 기록한다**(이 항목만 실행해도 저절로 드러나지 않는 종류다) |
| P7 | 라인 인용 오프셋 | 구현 중 실제 파일과 대조해 §1/§3 좌표를 최종 정정. 이미 확인된 소차: `xls_converter.py` 산출물 조회는 **:93**(문서 :97), `excel_gate.py` ref_set 합류는 **:189-198**(문서 :190-196), `build-bundle.sh` 사고 주석은 **:118-124** |
| P8 | V3② 오탐 축을 실행 가능한 형태로 고정 | 정상 `SUM` 을 `J9/H3` 와 **다른 시트**에 두거나, "SUM 셀 좌표가 ref_error cells 에 **없다**"는 좌표 단위 assert 로 적는다(같은 시트면 시트 단위로는 읽을 수 없다) |
| P9 | 임시물 assert 를 **집합 차분**으로 통일 | `xls2xlsx_*` 는 절대 존재검사, `excel_parser_rag_xls_*` 는 차분으로 서로 다르게 적혀 있다. 예전 실행이 남긴 stale 디렉터리에 무관하게 실패하지 않도록 **둘 다 전후 집합 차분**으로 쓴다 |
| P10 | V1 스텁 범위 | 스텁 백엔드가 만든 가짜 임시파일을 게이트가 열면 F10 경로로 `error` dict 노이즈가 난다 → `compute_gate_summary` 도 함께 스텁(기존 `test_parser_excel.py` 관례) |
