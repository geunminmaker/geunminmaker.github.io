# 조각조각 Android 앱 (APK) 폴더 안내

이 폴더는 **조각조각 웹앱을 안드로이드 앱처럼 설치할 수 있게 감싸 주는 "껍데기" 프로젝트**입니다.
기술 이름은 Trusted Web Activity(TWA)입니다.

- 앱 안에는 화면이나 기능 코드가 **없습니다.** 앱을 실행하면 휴대전화의 Chrome이 주소창 없이
  `https://geunminmaker.github.io/split-image/` 를 전체 화면으로 띄워 줍니다.
- 그래서 **웹사이트를 고치면 앱도 자동으로 최신 상태**가 됩니다. APK를 다시 만들 일은 거의 없습니다.
- APK를 다시 만들어야 하는 경우는 다음 정도뿐입니다.
  - 앱 이름, 아이콘, 시작 화면(스플래시), 시스템 바 색을 바꿀 때
  - 웹사이트 주소(호스트나 경로)가 바뀔 때
  - 안드로이드 정책 변경으로 `targetSdk` 를 올려야 할 때, 또는 라이브러리를 업데이트할 때

## 1. 앱 정보

| 항목 | 값 |
| --- | --- |
| 앱 이름 | 조각조각 |
| 패키지 이름(applicationId) | `io.github.geunminmaker.splitimage` |
| 버전 | 1.0.0 (versionCode 1) |
| 시작 주소 | `https://geunminmaker.github.io/split-image/?source=twa` |
| 지원 기기 | Android 7.0 이상 (minSdk 24, targetSdk 35) |
| 아이콘 배경색 | `#1F6BF0` |
| 시작 화면 / 시스템 바 색 | 밝은 테마 `#FFFFFF`, 어두운 테마 `#101214` |
| 서명 인증서 SHA-256 | `91:2E:8B:65:93:89:68:73:9F:FB:4C:8A:C7:F5:1C:1B:B7:D1:61:37:A6:9E:F2:36:0C:EF:B9:AF:00:64:E5:34` |

이 값들은 모두 `app/build.gradle` 맨 위의 `twa` 묶음과 `app/src/main/res/values/` 안에 있습니다.

## 2. 폴더 구성

| 경로 | 설명 | git에 올리나요? |
| --- | --- | --- |
| `app/build.gradle` | 앱 설정(이름 빼고 전부: 패키지, 주소, 버전, 서명) | 예 |
| `app/src/main/AndroidManifest.xml` | 안드로이드에 알려 주는 앱 구성 | 예 |
| `app/src/main/res/` | 아이콘, 시작 화면 그림, 색, 앱 이름 | 예 |
| `app/src/dev/` | 테스트용 DEV 앱의 이름 | 예 |
| `icon-src/` | 아이콘 원본 그림 | 예 |
| `tools/make_android_assets.py` | 원본 그림에서 모든 크기의 아이콘과 시작 화면 그림을 다시 만드는 스크립트 | 예 |
| `tools/build_apk.sh` | 빌드 + `dist/` 로 복사 + 서명 검증을 한 번에 하는 스크립트 | 예 |
| `assetlinks.json` | 웹사이트에 올려야 하는 "앱-사이트 연결 증명" 파일 (6번 참고) | 예 |
| `gradlew`, `gradlew.bat`, `gradle/` | 빌드 도구(Gradle) 실행 파일 | 예 |
| `signing/release.jks` | **서명 키(키스토어)** | **절대 안 됨** |
| `keystore.properties` | **서명 키의 비밀번호** | **절대 안 됨** |
| `local.properties` | 이 PC의 Android SDK 위치 | 아니요 |
| `dist/` | 완성된 APK와 확인용 화면 캡처 | 아니요 |

`dist/jogakjogak.apk` 가 사람들에게 나눠 주는 설치 파일입니다.

## 3. 서명 키 - 가장 중요한 부분

안드로이드는 **같은 키로 서명된 APK만 "같은 앱의 업데이트"로 인정**합니다.

- 키 파일: `android/signing/release.jks`
  (PKCS12 형식, 별칭 `jogakjogak`, RSA 2048비트, 2056년 11월 4일까지 유효)
- 비밀번호: `android/keystore.properties` 파일의 `storePassword=` 줄에 적혀 있습니다.
  (`keyPassword` 도 같은 값입니다. 무작위로 만든 32자입니다.)

**이 두 파일을 잃어버리면**

- 이미 설치된 앱을 더 이상 업데이트할 수 없습니다. 새 키로 만든 APK는 설치가 거부되므로,
  사용자가 기존 앱을 지우고 새로 설치해야 합니다.
- 6번의 `assetlinks.json` 도 새 키의 지문으로 바꿔서 다시 올려야 합니다.

**이 두 파일이 남에게 넘어가면** 다른 사람이 조각조각인 척하는 업데이트 APK를 만들 수 있습니다.

그래서 꼭 지켜 주세요.

1. **지금 바로 백업하세요.** `release.jks` 와 `keystore.properties` 를 함께, 이 PC가 아닌 곳 두 군데
   이상에 보관합니다(예: USB 메모리, 비밀번호 관리자의 첨부 파일, 본인만 보는 클라우드 폴더).
2. **절대 git에 커밋하지 마세요.** `android/.gitignore` 가 `signing/`, `keystore.properties`, `*.jks` 를
   제외하고 있습니다. `git status` 에 이 파일들이 보이면 안 됩니다.
3. 메신저, 메일, 공개 저장소, 이슈 글에 비밀번호를 붙여 넣지 마세요.

## 4. 다시 빌드하는 방법

필요한 것: JDK 17 이상(이 프로젝트는 JDK 21로 확인), Android SDK의 **Platform 36** 과 Build-Tools 35.0.0.
`targetSdk` 는 35이지만 사용하는 라이브러리가 컴파일용으로 **Platform 36을 요구**합니다.

### 방법 A - Android Studio (쉬운 방법)

1. Android Studio에서 **Open** 을 누르고 이 `android` 폴더를 엽니다.
2. 처음 열면 필요한 것을 내려받습니다. **SDK Platform 36이 없으면 Android Studio가 자동으로 설치**합니다
   (라이선스 동의 창이 뜨면 내용을 읽고 직접 동의하세요). `local.properties` 도 이 PC의 SDK 위치로
   자동으로 고쳐집니다.
3. 아래쪽 **Terminal** 탭에서 다음을 실행합니다.

   ```
   .\gradlew.bat :app:assembleRelease
   ```

4. 결과물: `app/build/outputs/apk/release/jogakjogak-1.0.0-release.apk`
   (`keystore.properties` 와 `signing/release.jks` 가 제자리에 있으면 자동으로 서명됩니다.)

### 방법 B - 명령줄 (Git Bash)

```
cd android
bash tools/build_apk.sh
```

이 스크립트는 빌드한 뒤 `dist/jogakjogak.apk`, `dist/jogakjogak-dev.apk` 로 복사하고, 서명(v1, v2, v3)과
앱 정보, `assetlinks.json` 의 지문이 맞는지까지 확인해 줍니다. 직접 Gradle만 돌리려면:

```
./gradlew.bat --no-daemon --console=plain :app:assembleRelease :app:assembleDev
```

### 알아 둘 점

- **`local.properties` 는 지금 임시 폴더의 SDK를 가리키고 있습니다.** 처음 만들 때 실제 Android SDK를
  건드리지 않으려고 임시 SDK를 썼기 때문입니다. 그 임시 폴더가 지워지면
  `SDK location not found` 또는 `android-36` 을 찾을 수 없다는 오류가 납니다.
  방법 A처럼 Android Studio로 한 번 열면 해결됩니다.
- **새 버전을 낼 때는 versionCode를 반드시 올리세요.** 같거나 낮으면 업데이트 설치가 거부됩니다.
  `app/build.gradle` 의 기본값을 고치거나 다음처럼 지정합니다.

  ```
  bash tools/build_apk.sh -PtwaVersionName=1.0.1 -PtwaVersionCode=2
  ```

- 서명 키가 없으면 빌드는 되지만 **서명되지 않은 APK**(`...-release-unsigned.apk`)가 나오고 설치할 수 없습니다.
- `AndroidManifest.xml` 의 `ManageDataLauncherActivity` 항목은 지우면 안 됩니다. 없으면 앱이 실행 즉시 종료됩니다.
- 확인된 조합: Gradle 8.13, Android Gradle Plugin 8.13.2, compileSdk 36, Build-Tools 35.0.0,
  androidbrowserhelper 2.7.4.

## 5. 아이콘, 시작 화면, 색 바꾸기

1. 원본 그림을 바꿉니다.
   - `android/icon-src/ic_launcher_foreground.png`, `ic_launcher_monochrome.png` (432 px, 적응형 아이콘 레이어)
   - `web/icons/icon-512.png` (둥근 사각형 아이콘), `web/icons/icon-1024.png` (꽉 찬 정사각형)
2. 프로젝트 맨 위 폴더에서 스크립트를 실행합니다.

   ```
   .venv\Scripts\python.exe android\tools\make_android_assets.py
   ```

   모든 해상도의 런처 아이콘(적응형 + 구형 + 원형)과 시작 화면 그림이 다시 만들어집니다.
   `--check` 를 붙이면 파일을 바꾸지 않고 최신인지만 확인합니다.
3. 색은 `app/src/main/res/values/colors.xml` (밝은 테마)과 `values-night/colors.xml` (어두운 테마의
   시작 화면 배경)에서 바꿉니다. **웹앱의 `theme-color` 와 같은 값으로 맞춰 주세요.**
4. 4번 방법으로 다시 빌드합니다(versionCode 올리기).

참고: 시작 화면이 사라진 뒤 **위쪽 상태 표시줄 색은 웹 페이지의 `<meta name="theme-color">` 를 따릅니다.**
APK에 넣은 색은 시작 화면과 아래쪽 내비게이션 바에 쓰입니다. 그래서 웹 쪽에도 밝은 테마 `#FFFFFF`,
어두운 테마 `#101214` 가 모두 들어 있어야 합니다(에뮬레이터의 Chrome 113에서 확인한 동작입니다).

## 6. assetlinks.json 과 전체 화면

앱이 주소창 없는 **전체 화면**으로 뜨려면 Chrome이 "이 앱과 이 웹사이트의 주인이 같다"는 것을 확인할 수
있어야 합니다. 그 증명이 `assetlinks.json` 입니다.

- 이 폴더의 `assetlinks.json` 을 **아래 주소에서 그대로 볼 수 있게** 올려야 합니다.

  ```
  https://geunminmaker.github.io/.well-known/assetlinks.json
  ```

- 주소의 **맨 위(루트)** 여야 합니다. `/split-image/.well-known/` 에 두면 소용이 없습니다.
  그래서 GitHub에는 `geunminmaker.github.io` 라는 이름의 저장소(사용자 사이트)가 필요합니다.
- GitHub Pages는 점(.)으로 시작하는 폴더를 기본으로 빼 버립니다. 사이트 맨 위에 빈 `.nojekyll` 파일을
  두거나, GitHub Actions로 배포한다면 `upload-pages-artifact` 에 `include-hidden-files: true` 를 줘야 합니다.
- 파일 안의 `sha256_cert_fingerprints` 는 3번 서명 키의 지문입니다. **키가 바뀌면 이 파일도 바꿔야 합니다.**
  `tools/build_apk.sh` 가 빌드할 때마다 지문이 맞는지 알려 줍니다.

확인에 실패하면(파일이 없거나 지문이 다르면) 앱이 망가지지는 않습니다. 다만 위쪽에 주소 표시줄이 있는
브라우저 탭 모양으로 열리고 "디지털 애셋 링크 확인에 실패함" 이라는 안내가 잠깐 보입니다.
파일을 올바로 올린 뒤 앱을 다시 실행하면 전체 화면으로 바뀝니다(반영까지 몇 분 걸릴 수 있습니다).

## 7. 테스트용 DEV 앱

`dist/jogakjogak-dev.apk` 는 개발 확인용입니다. 일반 사용자에게 나눠 주지 마세요.

- 이름 "조각조각 DEV", 패키지 `io.github.geunminmaker.splitimage.dev` (정식 앱과 나란히 설치 가능)
- 배포된 사이트 대신 `http://localhost:8765/` 를 엽니다. 에뮬레이터에서 PC의 개발 서버를 보려면:

  ```
  adb reverse tcp:8765 tcp:8765
  ```

- 다른 주소로 만들려면 `-PtwaDevUrl=http://localhost:포트/` 를 빌드 명령에 붙입니다.

## 8. 설치할 때 참고

- APK를 직접 설치하려면 휴대전화에서 "출처를 알 수 없는 앱 설치"를 허용해야 합니다.
  삼성 휴대전화는 "보안 위험 자동 차단" 설정 때문에 설치가 막힐 수 있습니다.
- 처음 실행할 때는 인터넷 연결이 필요합니다. Chrome(또는 TWA를 지원하는 브라우저)이 꺼져 있거나
  없으면 "사용할 수 없는 앱" 안내가 뜹니다.
- 가장 쉬운 설치 방법은 APK가 아니라 **Chrome에서 사이트를 열고 "앱 설치/홈 화면에 추가"** 를 누르는
  것입니다. APK는 원하는 사람을 위한 선택지로 두는 것을 권합니다.
