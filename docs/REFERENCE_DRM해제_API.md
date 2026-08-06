# DRM 해제 API 명세서

타 서버(외부 시스템·워크플로우) 개발자가 SPS 포탈을 통해 Fasoo DRM(`.fsd`) 파일을 해제(복호화)하기 위한 API.

| 항목 | 값 |
|---|---|
| Base URL (운영) | `http://13.209.146.36` (포트 80, HTTP) |
| 인증 | `Authorization: Bearer <API_KEY>` (JWT 불필요) |
| Content-Type | `multipart/form-data` |

---

## 1. DRM 해제

`.fsd` 파일을 받아 평문 원본 바이너리를 반환한다.

```
POST /api/drm/agent/tool/unpack
```

### 요청

| 구분 | 이름 | 필수 | 설명 |
|---|---|---|---|
| Header | `Authorization` | O | `Bearer <API_KEY>` |
| Part (multipart) | `file` | O | DRM 원본 파일 (`.fsd` 등) |

- 최대 크기: 100MB
- part 이름은 반드시 `file`

### 응답

| 코드 | 의미 | 본문 |
|---|---|---|
| `200 OK` | 해제 성공 | 평문 파일 바이너리 |
| `400 Bad Request` | 빈 파일(`file` 파트는 있으나 내용 0바이트) | 없음 |
| `401 Unauthorized` | Bearer 누락·불일치 | 없음 |
| `422 Unprocessable Entity` | DRM 해제 실패(키 불일치 등) | 없음 |
| `500 Internal Server Error` | 서버 내부 오류 / `file` 파트 자체 누락 | JSON 또는 없음 |

성공 응답 헤더:
- `Content-Type`: 확장자별 자동 감지 (pdf/docx/xlsx/pptx/doc/xls/ppt/hwp/hwpx → 해당 MIME, 그 외 `application/octet-stream`)
- `Content-Disposition: attachment; filename*=UTF-8''<원본파일명>`

> DRM 파일이 아니면 해제하지 않고 **입력 바이트를 그대로** 반환한다(폴백). 실제 해제 여부는 반환 바이너리로 판단.