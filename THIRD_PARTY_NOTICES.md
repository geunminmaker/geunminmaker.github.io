# 오픈소스 라이선스 고지 (Third-party notices)

조각조각은 아래 오픈소스 소프트웨어를 포함하거나 사용합니다. 각 소프트웨어의 저작권은 해당 저작권자에게 있으며,
라이선스 전문은 [`web/licenses/`](web/licenses/README.txt) 폴더에 들어 있고 앱과 함께 배포됩니다(앱의 "오픈소스 라이선스" 화면에서 열립니다).

이 저장소의 코드(조각조각 자체)는 **GNU Affero General Public License v3.0** ([LICENSE](LICENSE))으로 공개합니다.
PDF 처리를 맡는 PyMuPDF/MuPDF가 AGPL이기 때문입니다. 앱의 전체 소스 코드는 이 저장소에서 받을 수 있습니다.

## 앱에 포함되어 사용자 기기에서 실행되는 것

| 소프트웨어 | 용도 | 라이선스 | 출처 |
| --- | --- | --- | --- |
| Pyodide 314.0.7 | 브라우저에서 Python 실행 | MPL-2.0 | https://github.com/pyodide/pyodide |
| CPython 3.14 (Pyodide에 포함) | Python 인터프리터와 표준 라이브러리 | PSF License 2.0 | https://www.python.org |
| Pillow 12.2.0 | 이미지 읽기·확대·저장 | MIT-CMU (HPND) | https://github.com/python-pillow/Pillow |
| PyMuPDF 1.27.2.2 / MuPDF | PDF 읽기, 벡터 분할, 일부 이미지 디코딩 | AGPL-3.0 | https://github.com/pymupdf/PyMuPDF · https://mupdf.com |
| olefile 0.47 | HWP·구형 Office 파일 구조 읽기 | BSD-2-Clause | https://github.com/decalage2/olefile |
| pillow-heif 1.3.0 | HEIC 사진 읽기 | BSD-3-Clause | https://github.com/bigcat88/pillow_heif |
| libheif, libde265 (pillow-heif에 포함) | HEIC 디코딩 | LGPL-3.0 | https://github.com/strukturag/libheif · https://github.com/strukturag/libde265 |
| Emscripten 런타임 (Pyodide에 포함) | WebAssembly 실행 환경, C 라이브러리 | MIT / NCSA | https://github.com/emscripten-core/emscripten |

위 패키지에는 다음 라이브러리가 정적으로 포함되어 있습니다.

- Pillow: libjpeg-turbo, zlib, FreeType, libwebp, libtiff
- PyMuPDF/MuPDF: FreeType, HarfBuzz, OpenJPEG, libjpeg, zlib, Little CMS(lcms2mt), Brotli, jbig2dec(AGPL-3.0), Gumbo(Apache-2.0), ZXing-C++·Tesseract(Apache-2.0), Leptonica(BSD-2-Clause)
- pillow-heif: libheif, libde265 (LGPL-3.0)

각 라이브러리의 라이선스 전문과 소스 주소는 `web/licenses/README.txt`에 정리되어 있습니다.

## Android 앱(APK)

| 소프트웨어 | 용도 | 라이선스 | 출처 |
| --- | --- | --- | --- |
| android-browser-helper 2.7.4 | Trusted Web Activity 실행 | Apache-2.0 | https://github.com/GoogleChrome/android-browser-helper |
| AndroidX (browser, core, appcompat) | 위 라이브러리의 의존성 | Apache-2.0 | https://developer.android.com/jetpack/androidx |

## 한글 문서 형식

본 제품은 한글과컴퓨터의 한글 문서 파일(.hwp) 공개 문서를 참고하여 개발하였습니다.
HWPX 쓰기는 공개 표준 KS X 6101(OWPML)을 따릅니다.

## 테스트에만 쓰는 자료 (앱에는 포함되지 않음)

`tests/fixtures/hangul/`의 한글 문서는 Apache-2.0 라이선스의 공개 프로젝트
(neolord0/hwplib, neolord0/hwpxlib, hahnlee/hwp.js, airmang/python-hwpx) 테스트 자료를 그대로 가져온 것입니다.
출처는 `tests/fixtures/hangul/SOURCES.md`에 적혀 있습니다.

개발·테스트 도구(pytest, Playwright, python-docx, python-pptx, pypdf 등)는 배포물에 포함되지 않습니다.
