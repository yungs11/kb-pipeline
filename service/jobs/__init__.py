"""facade 잡 큐 — 동시처리·유량제어 소유 모듈.

설계: ``docs/superpowers/specs/2026-08-03-facade-job-queue-design.md``
범위 밖 항목: ``docs/superpowers/specs/2026-08-03-facade-job-queue-deferred.md``

facade API 는 잡을 접수만 하고(밀리초), 다운스트림(parse-svc/adaptive_chunk/edgequake)
호출은 별도 프로세스(``python -m service.worker``)의 슬롯 안에서만 일어난다. 조율은
postgres ``kbp`` 스키마 하나로만 한다 — 메모리 공유도, 브로커도 없다.
"""
