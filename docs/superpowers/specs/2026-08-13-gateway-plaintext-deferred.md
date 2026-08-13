# deferred — 게이트웨이 평문 전송 (2026-08-13)

## D1. `KBP_PADDLE_OCR_GATEWAY_URL` 이 HTTPS → HTTP 로 다운그레이드됐다

2026-08-13 주소 이관에서 리포 커밋값이 바뀌었다(`3430cec`).

```
구  https://api-doc.ys-helperai.com/ocr/paddleocr_vl   TLS
신  http://15.164.81.29:18081/ocr/paddleocr_vl         평문
```

**실측**: 신 호스트는 `:18081`·`:443` 둘 다 TLS 미지원(`curl -k` → 000).
**구 호스트는 아직 살아 있다**(405 = 엔드포인트 존재).

## 왜 문제인가

paddle_gw 레인이 보내는 것은 **페이지 이미지 전체**다. 이 파이프라인의 실제 코퍼스에는
등기부등본·소송 서류처럼 **주민등록번호·성명·주소가 든 문서**가 있다(degen 픽스처 선정
때 실측 확인 — 그래서 코퍼스를 리포에 커밋하지 않는다). 공인 IP 로 평문 전송이면
그 이미지가 그대로 노출된다.

노출면은 커밋된 기본값 2곳이다:
- `docker-compose.yml:315`  `${KBP_PADDLE_OCR_GATEWAY_URL:-http://15.164.81.29:18081/...}`
- `.env.example` · `scripts/parse-svc.env.example`

## 현재 처분

**사용자 결정으로 신 주소 유지**(2026-08-13, "앞으론 저 주소임" + 평문 사실을 고지한 뒤 "진행해").
백그라운드 보안 리뷰도 같은 지적을 냈다(Plaintext Transport in docker-compose.yml).

## 선택지 (다시 볼 때)

1. **게이트웨이에 TLS 를 붙인다** — 근본 해결. 서버 쪽 작업이라 이 리포 범위 밖.
2. 구 호스트(`api-doc.ys-helperai.com`)로 되돌린다 — TLS 는 되나 사용자 결정에 반한다.
3. 사설망/VPN 안에서만 접근하도록 배선 — 평문이어도 노출면이 사라진다.

**폐쇄망은 영향 없다** — `.env.airgap.example` 은 `host.containers.internal:18081`(현장 로컬),
`docker-compose.airgap.yml` 은 공란이라 이 커밋의 기본값을 타지 않는다.
