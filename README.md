# 조각조각 — 이미지 분할

사진이나 문서 속 그림 한 장을 2~18조각으로 똑같이 나누고, 각 조각을 원본 크기에 맞게 키워서
PDF · JPG · PNG · Word · PowerPoint · 한글 파일로 돌려주는 앱입니다.
A4 한 장짜리 그림을 9장으로 나눠 인쇄해 큰 포스터를 만들 때 쓰기 좋습니다.

**앱 열기: https://geunminmaker.github.io/split-image/**

- iPhone · Android · PC에서 모두 동작하고, 홈 화면에 설치할 수 있습니다. → [설치 안내](docs/설치안내.md)
- 서버가 없습니다. 고른 파일은 기기 안에서만 처리되고 어디에도 전송되지 않습니다.
- 한 번 써 본 뒤에는 인터넷 없이도 쓸 수 있습니다. (PDF·구형 한글(.hwp)·HEIC는 처음 열 때 도구를 한 번 더 받습니다.)

## 쓰는 방법

1. 사진이나 파일(JPG, PNG, PDF, 한글, Word, PowerPoint …)을 고릅니다.
2. 찾은 이미지가 맞는지 확인합니다. 여러 장이면 한 장을 고릅니다.
3. 몇 조각으로 나눌지(2·4·6·8·9·12·16·18)와 나누는 모양을 미리보기로 정합니다.
4. 저장 형식을 고르고 저장합니다.

제품 요구사항과 동작 규칙은 [PRD.md](PRD.md)에 있습니다.

## 구조

| 폴더 | 내용 |
| --- | --- |
| `splitcore/` | Python 엔진. 분할 규칙, 파일 읽기(사진·PDF·Office·한글·ZIP), 저장(PDF·JPG·PNG·DOCX·PPTX·HWPX), 명령줄 |
| `web/` | 설치형 웹앱. 화면(HTML/CSS/JS), 서비스 워커, 브라우저용 Python(Pyodide) |
| `android/` | Android APK(Trusted Web Activity) 프로젝트 |
| `tests/` | 엔진 단위 테스트, 형식별 결과 검증, 실제 브라우저 테스트 |
| `tools/` | 빌드·검증 스크립트 |

분할과 변환은 전부 Python(`splitcore`)이 합니다. 휴대폰에서는 같은 코드가 Pyodide(WebAssembly로 만든 Python) 위에서 실행됩니다.

## PC에서 명령줄로 쓰기

```
python -m venv .venv
.venv\Scripts\pip install pillow pymupdf olefile
.venv\Scripts\python -m splitcore 포스터.pdf                      # 앱과 같은 질문을 차례로 묻습니다
.venv\Scripts\python -m splitcore 사진.jpg -n 9 -f pdf -o 결과 --yes  # 묻지 않고 바로 실행
```

## 개발

```
.venv\Scripts\pip install pillow pymupdf olefile python-docx python-pptx pypdf pytest playwright
.venv\Scripts\python -m playwright install chromium webkit

.venv\Scripts\python tools\build_web.py        # 엔진을 web/ 에 묶기
.venv\Scripts\python tools\serve.py 8765       # http://localhost:8765/ 에서 앱 열기

.venv\Scripts\python tools\make_corpus.py --office   # 테스트용 이미지·문서 만들기 (--office 는 MS Office가 있을 때)
.venv\Scripts\python -m pytest tests -q               # 전체 테스트 (브라우저 테스트 포함)
.venv\Scripts\python tools\verify_office.py           # 결과 Word·PowerPoint 파일을 실제 Office로 열어 확인 (Windows)
```

`main` 브랜치에 올리면 GitHub Actions가 사이트를 다시 배포합니다(`.github/workflows/pages.yml`).
저장소를 새로 만들 때는 Settings > Pages > Source를 "GitHub Actions"로 한 번 바꿔야 합니다.
배포한 뒤에는 `python tools/check_live.py`로 주소·파일 형식·해시를, `python tests/e2e/live_check.py`로 실제 동작을 확인합니다.

## 라이선스

이 저장소의 코드는 [GNU AGPL v3.0](LICENSE)으로 공개합니다. PDF 처리에 쓰는 PyMuPDF/MuPDF가 AGPL이기 때문입니다.
포함된 오픈소스 목록은 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)에 있습니다.

본 제품은 한글과컴퓨터의 한글 문서 파일(.hwp) 공개 문서를 참고하여 개발하였습니다.
