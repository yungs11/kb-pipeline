# Legacy prior-label join correction

`sample.tsv`의 과거 `prior_gw_label`과 `prior_reason`은 이 실험의 현재 doc/page에 대한
label이 아니다. `gw_labels.tsv.idx`는 과거 audit identity를 가리키지만, harness가 이를
현재 `qual_sample_60p_normal.tsv` 행 번호로 잘못 해석해 positional join했다.

대표 오류:

- 잘못 표시된 대상: I19, 양주시 옥정동 문서 p35
- 붙어 있던 문구: `한자쓰레기+신탁계약→신학계악, 수익자→수락자`
- 실제 과거 근거: 서울시 종로구 신문로 문서 p141의 300 DPI GW output
- 실제 raw file: `/Users/xxx/workspace/9.kbp-parser-compare/results/raw/gw/01_서울시_종로구_신문로_대리사무_분관신_서울중앙_부당이득금_주_p141.md`

따라서 `sample.tsv`의 두 legacy 열은 감사 목적으로만 보존하고 열 이름에 `_INVALID`를
붙였다. 표본 strata, 현재 parser 품질, human label 또는 정책 metric의 근거로 사용하지 않는다.

영향받지 않는 산출물:

- 저장된 동일 원본 이미지와 SHA-256
- 현재 parser의 GW/VL raw output
- 16/16 원본 대조 `human_review.tsv`
- human-grounded policy comparison

영향받는 해석:

- 사전등록의 `known GW strata` 구성
- `prior GW label`이라는 REVIEW 표시
- 표본을 사전에 검증된 error-strata enrichment라고 부르는 설명

이 표본은 이후 **convenience DEV sample**로만 취급한다. 원본 대조 후 실제로 GW critical
error 11/16이 관찰됐지만, 이는 사전에 올바른 identity label로 구성한 strata가 아니다.
