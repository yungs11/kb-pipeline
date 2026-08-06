# API 정의서 — 에이전트 파일 변환

## 1. 개요

원본 문서 파일을 업로드하면 한컴 도큐먼트툴즈를 통해 PDF 로 변환하고, 변환 결과를 내려받는 API 이다.
변환 요청은 동기 방식으로 처리되며, 응답 수신 시점에 변환이 완료되어 있으므로 상태 폴링이 필요하지 않다.


### 1.1 처리 흐름

| 순서 | 단계 | API |
|---|---|---|
| 1 | 파일 업로드 및 변환 | `POST /convert-sync` |
| 2 | 변환 결과 다운로드 | `GET /download/{cnvId}` |

---

## 2. 공통 규격

### 2.1 기본 정보

| 항목 | 값 |
|---|---|
| 프로토콜 | HTTP |
| 서버 | `13.209.146.36` (EC2 DEV) |
| 포트 | `80` |
| Base URL | `http://13.209.146.36/api/fileconvert/agent/tool` |
| 문자 인코딩 | UTF-8 |

> 외부 개방 포트는 `80` 이다. `8080` · `8201` 은 보안그룹에서 차단되어 있다.
> storage-app 에 직접 연결이 필요한 경우 SSH 터널을 사용한다.
> `ssh -i ~/.ssh/id_ed25519 -L 8201:172.31.43.198:8201 ubuntu@13.209.146.36`

### 2.2 인증

| 항목 | 값 |
|---|---|
| 방식 | Bearer Token |
| 헤더 | `Authorization: Bearer {TOKEN}` |
| 인증 실패 응답 | HTTP `401` (응답 본문 없음) |

인증은 변환 요청(`POST /convert-sync`) 에만 적용된다. 다운로드(`GET /download/{cnvId}`) 는 인증 없이 호출 가능하다.

### 2.3 공통 에러 응답

변환 요청 처리 중 발생한 예외는 아래 형식으로 반환된다.

```json
{
  "errorCode": "E000001",
  "errorMsg": "필수 파일이 첨부되지 않았습니다.",
  "data": null
}
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `errorCode` | String | 에러 코드 |
| `errorMsg` | String | 에러 메시지 |
| `data` | Object | 항상 `null` |

### 2.4 공통 에러 코드

| 에러 코드 | HTTP | 메시지 | 발생 조건 |
|---|---|---|---|
| `E000001` | `200` | 필수 파일이 첨부되지 않았습니다. | `file` 파트 누락 |
| `E000007` | `500` | HttpMediaTypeNotSupportedException | `Content-Type` 이 `multipart/form-data` 가 아님 |
| `E000007` | `500` | NotFound: 404 Not Found | 존재하지 않는 `cnvId` 다운로드 |
| — | `401` | (본문 없음) | 토큰 누락 또는 불일치 |
| — | `422` | (변환 응답 본문 참조) | 지원하지 않는 입력 확장자 |

> 일부 오류는 HTTP `200` 으로 반환된다. 성공 여부는 HTTP 상태 코드가 아니라 응답 본문의 `success` 또는 `errorCode` 필드로 판정해야 한다.

---

## 3. API 상세

### 3.1 파일 변환 요청

| 항목 | 내용 |
|---|---|
| 기능명 | 파일 업로드 및 PDF 변환 |
| URL | `/api/fileconvert/agent/tool/convert-sync` |
| Method | `POST` |
| Content-Type | `multipart/form-data` |
| 인증 | 필요 |
| 처리 방식 | 동기 (변환 완료 후 응답) |

#### 3.1.1 요청 헤더

| 헤더명 | 필수 | 값 | 설명 |
|---|---|---|---|
| `Authorization` | Y | `Bearer {TOKEN}` | 인증 토큰 |
| `Content-Type` | Y | `multipart/form-data` | 고정 |

#### 3.1.2 요청 파라미터

| 파라미터 | 타입 | 필수 | 전달 위치 | 기본값 | 설명 |
|---|---|---|---|---|---|
| `file` | File | Y | Form Data | — | 변환 대상 원본 파일. 파트명은 `file` 고정 |
| `crtId` | String | N | Form Data / Query | `null` | 생성자 식별자(사번). 로깅 및 이력 관리용 |
| `targetFormat` | String | N | Form Data / Query | `pdf` | 출력 포맷. **현재 미사용** — 값과 무관하게 PDF 로 변환됨 |

#### 3.1.3 지원 입력 확장자

| 구분 | 확장자 |
|---|---|
| 한글 | `hwp`, `hwpx` |
| 워드 | `doc`, `docx` |
| 엑셀 | `xls`, `xlsx` |
| 파워포인트 | `ppt`, `pptx` |
| 웹 문서 | `html`, `htm` |
| 이미지 | `jpg`, `jpeg`, `png`, `gif`, `bmp`, `tiff`, `tif` |
| 문서 | `pdf` |

정의 위치: [ConversionFormat.java](app/src/main/java/com/shinhan/storage/config/ConversionFormat.java)
목록에 없는 확장자(`txt` 등)를 입력하면 HTTP `422` 로 실패한다.

#### 3.1.4 응답 본문

| 필드 | 타입 | 설명 |
|---|---|---|
| `success` | Boolean | 변환 성공 여부 |
| `cnvId` | Number | 변환 식별자. 다운로드 시 사용 |
| `fileName` | String | 변환 결과 파일명 (`{원본명}.pdf`) |
| `downloadUrl` | String | 다운로드 경로 (호스트 기준 절대 경로) |
| `message` | String | 처리 결과 메시지 |

#### 3.1.5 응답 예시 — 성공 (`200 OK`)

```json
{
  "success": true,
  "cnvId": 621,
  "fileName": "sample.pdf",
  "downloadUrl": "/api/fileconvert/agent/tool/download/621",
  "message": "변환이 완료되었습니다."
}
```

#### 3.1.6 응답 예시 — 실패

지원하지 않는 확장자 (`422`)

```json
{
  "success": false,
  "cnvId": null,
  "fileName": null,
  "downloadUrl": null,
  "message": "변환 제출 실패: 400 Bad Request from POST http://172.31.43.198:8201/api/fileconvert/agent/tool/convert-sync"
}
```

파일 미첨부 (`200`)

```json
{
  "errorCode": "E000001",
  "errorMsg": "필수 파일이 첨부되지 않았습니다.",
  "data": null
}
```

#### 3.1.7 호출 예시

```bash
curl -X POST "http://13.209.146.36/api/fileconvert/agent/tool/convert-sync" \
  -H "Authorization: Bearer {TOKEN}" \
  -F "file=@sample.hwp" \
  -F "crtId=dh.kim"
```

---

### 3.2 변환 결과 다운로드

| 항목 | 내용 |
|---|---|
| 기능명 | 변환 결과 파일 다운로드 |
| URL | `/api/fileconvert/agent/tool/download/{cnvId}` |
| Method | `GET` |
| 인증 | 불필요 |

#### 3.2.1 요청 파라미터

| 파라미터 | 타입 | 필수 | 전달 위치 | 설명 |
|---|---|---|---|---|
| `cnvId` | Number | Y | Path | convert-sync 응답의 `cnvId` |

#### 3.2.2 응답 (`200 OK`)

| 항목 | 값 |
|---|---|
| `Content-Type` | `application/pdf` |
| `Content-Disposition` | `attachment; filename*=UTF-8''converted_{cnvId}.pdf` |
| Body | PDF 바이너리 (`%PDF-1.4` 로 시작) |

다운로드 파일명은 원본 파일명이 아닌 `converted_{cnvId}.pdf` 로 고정된다.

#### 3.2.3 응답 — 실패 (`500`)

```json
{
  "errorCode": "E000007",
  "errorMsg": "NotFound: 404 Not Found from GET http://172.31.43.198:8201/api/fileconvert/agent/tool/999999/download",
  "data": null
}
```

#### 3.2.4 호출 예시

```bash
curl -o result.pdf \
  "http://13.209.146.36/api/fileconvert/agent/tool/download/621"
```

---

## 4. 연동 예제

### 4.1 Shell

```bash
TOKEN="{TOKEN}"
BASE="http://13.209.146.36/api/fileconvert/agent/tool"

RES=$(curl -s -X POST "$BASE/convert-sync" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@sample.hwp" \
  -F "crtId=dh.kim")

CNV_ID=$(echo "$RES" | jq -r '.cnvId')
curl -s -o result.pdf "$BASE/download/$CNV_ID"
```

## 5. 제약 사항

| 번호 | 항목 | 내용 |
|---|---|---|
| 1 | 출력 포맷 | PDF 고정. `targetFormat` 파라미터는 전달되어도 적용되지 않는다. |
