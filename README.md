# Rockchip RSS Summary Notifier

여러 RSS에서 같은 사건을 AI로 판별하고, 설정한 수 이상의 **서로 다른 소스**가 확인되면 한국어 두괄식·불릿 요약을 stdout과 Telegram으로 전송합니다. Python 3.11 이상, 런타임 Python 외부 의존성 없음.

## 시작하기

```sh
python3 -m rss_notifier --check
# 별도로 실행한 Ollama 서버에 설정 파일의 모델을 준비하세요.
ollama pull qwen2.5:3b
export TELEGRAM_BOT_TOKEN='봇 토큰'
python3 -m rss_notifier --once
python3 -m rss_notifier
```

실행 위치는 프로젝트 루트입니다. Telegram 봇을 준비하고 봇 채팅에서 `/start`를 보내 구독하세요. chat ID 수동 설정은 필요하지 않습니다. 비밀값은 환경 변수로만 주입하며 저장소에 기록하지 않습니다. Telegram 없이 시험하려면 설정에서 telegram dispatcher 블록을 제거하세요.

`configs/news.toml`에는 요청한 연합뉴스TV, SBS, MBC, 조선일보, JTBC 주소와 두 dispatcher가 등록되어 있습니다. 기본 역치는 서로 다른 소스 3개, AI 판별 점수 0.8, 최근 24시간입니다. 점수는 모델의 판단값이며 통계적으로 보정된 확률이 아닙니다. 연합뉴스TV 주소는 조사 환경에서 403이 발생했습니다. 실제 장치에서도 접근 오류가 발생하면 로그를 확인하고 서비스가 제공하는 현재 RSS 주소로 교체해야 합니다.

## 프리셋

`[[presets]]`와 그 하위 `feeds`, `dispatchers` 블록을 복제해 다른 이름으로 추가합니다. 기본 실행은 활성화된 모든 프리셋을 한 프로세스에서 순서대로 처리합니다. RK3588 메모리 사용을 줄이기 위해 추론은 직렬 실행합니다.

```sh
python3 -m rss_notifier --preset korean-news --preset another-preset
python3 -m rss_notifier --config configs/news.toml --once
```

소스 ID는 언론사 등 독립된 소스를 나타내야 합니다. 같은 언론사의 URL 여러 개를 서로 다른 소스 ID로 등록하면 독립 소스로 잘못 집계됩니다. 프리셋 이름과 dispatcher ID는 상태 식별자이므로 변경 시 별도 작업으로 취급됩니다. 주기 기본값은 300초이며 실제 주기는 전체 처리 시간 + 대기 시간입니다.

RSS/Atom의 제목과 제공된 본문·설명만 처리합니다. 원문 페이지를 별도로 긁지 않으므로 RSS가 짧은 설명만 주면 요약 정보량에도 한계가 있습니다. 유사도는 제목을 AI로 비교합니다. 새 항목은 최근 기사와 비교하여 처음 역치를 통과한 사건에 들어가고, 같은 소스의 여러 기사는 소스 수를 늘리지 않습니다. 서로 다른 소스가 아직 없는 단독 기사도 다음 주기까지 보관됩니다. 이미 전송한 사건과 일치하면 유효 시간창 안에서 재알림하지 않습니다. 초기 실행도 최근 시간창 내 기사들을 신규로 취급합니다. 날짜가 없거나 해석 불가능한 항목은 최초 수집 시간을 사용합니다.

## RK3588: 긴 입력과 출력

기본 추론 서버는 Ollama HTTP API입니다. Ollama를 사용할 수 있는 ARM64 환경에서 CPU 추론으로 실행할 수 있습니다. 이 프로젝트는 RKNN/NPU 드라이버나 모델 변환을 포함하지 않습니다. 기존 RKLLM 서버가 OpenAI 호환 API를 제공하면 아래처럼 연결할 수 있으나 실제 지원 파라미터와 모델의 컨텍스트 한도를 먼저 확인해야 합니다.

```toml
[model]
backend = "openai"
base_url = "http://127.0.0.1:8080/v1"
model = "your-served-model"
context_tokens = 8192
max_output_tokens = 2048
timeout_seconds = 600
# api_key_env = "LOCAL_MODEL_API_KEY"
```

Ollama에는 `num_ctx`, `num_predict`, 호환 API에는 `max_tokens`를 명시합니다. 호환 API 서버의 컨텍스트 크기는 서버 쪽에서도 설정해야 합니다. 설정으로 모델의 학습 한도나 장치 메모리 한도를 늘릴 수는 없습니다. 기본 모델명은 시작용 예시이며 RK3588에서 성능을 실측한 추천값은 아닙니다.

긴 기사 → 입력 예산 내 조각별 압축 → 재귀 통합 → 기사별 근거 통합 순서로 처리합니다. 한 번에 원문 전체를 넣는 대신 각 호출을 제한합니다. 한국어에도 보수적으로 동작하도록 UTF-8 바이트 수를 토큰 상한 추정에 사용하며 출력 공간과 시스템 지시 여유를 미리 제외합니다. 토크나이저에 따라 정확한 토큰 수는 다르므로 대상 모델에서 확인해야 합니다. 중간 압축이 줄어들지 않거나 출력 토큰 제한에 걸리면 부분 결과를 전송하지 않고 다음 주기에 재시도합니다.

`context_tokens`와 `max_output_tokens`를 함께 늘리면 호출당 더 긴 결과를 받을 수 있지만 KV 캐시 메모리와 지연이 늘어납니다. 전체 입력량은 계층 요약으로 처리하며 이 과정은 손실 압축입니다. 무제한 원문 기억이나 무제한 최종 출력은 보장하지 않습니다. 최종 요약은 출력 예산 안에서 작성되고 출처 목록은 별도로 붙습니다. Telegram 전송은 UTF-16 기준 3500단위 이하로 나눕니다.

장치 검증 순서:

1. RAM·swap 여유를 확인하고 기본 8192/2048 설정으로 짧은 기사 두 개를 테스트합니다.
2. 실제 긴 한국어 RSS 데이터로 처리 시간, 메모리 최대치, 수치·고유명사 보존을 확인합니다.
3. 16384/4096 등으로 단계적으로 늘리고 서버 로그에서 적용 컨텍스트와 출력 잘림을 확인합니다.
4. 서버가 지원하지 않거나 메모리가 부족하면 호출 크기를 낮추고 계층 요약을 사용합니다. 추론 성능이 부족하면 같은 API의 외부 서버로 옮길 수 있습니다.

관련 명세: [Ollama chat API](https://docs.ollama.com/api/chat), [컨텍스트 설정](https://docs.ollama.com/context-length), [Telegram sendMessage](https://core.telegram.org/bots/api#sendmessage).

## 상태·실패 처리

SQLite에는 기사, 사건, 유사도 캐시, 생성한 요약, dispatcher별 전송 결과를 저장합니다. 재시작해도 처리 상태가 유지되며 실패한 dispatcher만 재시도합니다. Telegram은 조각마다 진행 상태를 기록합니다. 원격 전송 성공 직후 로컬 저장 전에 종료되면 일부 중복 전송될 수 있는 at-least-once 방식입니다. 피드 장애는 다른 피드 수집을 막지 않고, 프리셋 실패는 다음 프리셋 실행을 막지 않습니다. 운영 로그는 stderr, 요약은 stdout으로 출력합니다. 장기 운영 시 DB 크기를 관찰하세요. 자동 보존기간 삭제는 아직 없으며 과거 상태 삭제 시 중복 방지 정보도 함께 사라질 수 있습니다. 같은 DB를 여러 프로세스에서 동시에 실행하지 마세요.

AI 호출 수는 시간창 내 기사 조합에 따라 커집니다. 초기 수집량이 많으면 처리에 오래 걸릴 수 있습니다. 모델의 사건 판별 정확성과 요약 품질은 별도 실데이터 평가가 필요합니다. 현재 테스트는 모의 모델·모의 RSS를 사용하며 실제 RK3588, 실시간 모델 추론 및 Telegram 실전송은 검증하지 않았습니다.

## 테스트

```sh
python3 -m unittest discover -s tests -v
python3 -m rss_notifier --check
```

새 dispatcher는 `send(text, offset=0, checkpoint=None)`을 구현하고 `DISPATCHERS`에 팩터리를 등록합니다. 긴 메시지는 조각 전송 후 `checkpoint(다음 조각 번호)`를 호출해 재시작 위치를 저장합니다.

## Podman으로 Orange Pi에 배포

`Containerfile`은 ARM64 지원 Python 3.12 이미지로 앱을 실행합니다. 호스트 Python 3.10과 무관하게 동작하며, Python 외부 패키지를 설치하지 않습니다.

서버에서 Podman을 설치한 뒤 일반 사용자로 실행합니다.

```sh
sudo apt-get update
sudo apt-get install -y podman uidmap slirp4netns fuse-overlayfs
cd ~/workspace/rockchip-summary-notifier
./deploy/install.sh
```

설치 스크립트는 앱 이미지를 빌드하고, 루트리스 Ollama 컨테이너와 알림 컨테이너를 사용자 systemd 서비스로 실행합니다. Ollama 모델은 `qwen2.5:3b`를 다운로드하며 CPU로 추론합니다. NPU 가속은 포함하지 않습니다. 기존 11434 포트 모델 서버가 있으면 모델 서비스를 시작하지 말고 해당 서버를 사용하도록 조정하세요.

Telegram 토큰은 서버의 `deploy/runtime.env`에 입력합니다(`runtime.env.example` 참고). 토큰 없이 Telegram을 활성화하면 실행할 수 없습니다. chat ID 환경 변수는 필요하지 않습니다. 설정은 최초 설치 시 생성되는 `configs/server.toml`을 수정합니다. 재배포 시 이 두 파일과 `state/`는 덮어쓰지 않습니다.

```sh
systemctl --user restart rss-notifier
journalctl --user -u rss-notifier -f
systemctl --user status rss-notifier-model rss-notifier
podman exec rss-notifier-model ollama list
```

서비스는 로그인 시 자동 시작합니다. 로그인 없이 재부팅 직후에도 시작하려면 서버에서 `sudo loginctl enable-linger orangepi`를 한 번 실행해야 합니다. `loginctl show-user orangepi -p Linger`로 확인합니다. 종료는 `systemctl --user stop rss-notifier rss-notifier-model`입니다.

컨테이너는 호스트 네트워크를 사용해 localhost 모델 서버에 연결합니다. 앱 파일 시스템은 읽기 전용이고 SQLite 상태만 `state/`에 씁니다. 모델 데이터는 Podman 볼륨 `rss-notifier-models`에 남습니다. 이미지 업데이트는 `./deploy/install.sh` 실행 후 `systemctl --user restart rss-notifier`로 적용합니다.


## Telegram 구독

- `/start`: 이 봇에 연결된 활성 프리셋 전체 구독
- `/start korean-news`: 지정 프리셋 구독
- `/stop`: 이 봇의 전체 구독 해제
- `/stop korean-news`: 지정 프리셋 구독 해제

초대코드는 사용하지 않습니다. 정원은 전체 DB에서 고유 chat ID **1,000개**입니다. 한 채팅의 복수 프리셋 구독은 한 자리로 계산합니다. 정원이 차면 새 chat ID의 구독을 저장하지 않고 “지금은 처리할 수 없습니다. 구독자 정원(1,000개 채팅)이 찼습니다.”라고 응답합니다. 기존 구독자의 `/start`는 정원에서도 허용하며 전체 구독 해제 시 자리가 비워집니다.

구독, 수신 update offset, 구독자별 전송 조각과 완료 상태를 SQLite에 보관합니다. AI 추론과 독립된 작업자가 명령과 전송 큐를 처리합니다. Telegram dispatcher의 완료는 구독자별 큐 저장 완료를 의미하며, 실제 전송 실패는 큐에서 재시도합니다. 구독자가 없을 때 생성된 요약은 이후 가입자에게 소급 전송하지 않습니다. `/stop`은 해당 구독의 대기 요약도 삭제합니다. 해제 후 재구독해도 예전 요약을 다시 보내지 않습니다.

한 봇의 전송은 약 초당 16건 이하, 같은 채팅에는 1.1초 이상의 간격을 둡니다. Telegram 429는 서버가 지정한 시간 뒤 재시도하고, 403은 해당 채팅의 구독을 삭제합니다. 다른 오류는 60초 후 재시도하며 정상 구독자 처리는 계속합니다. 1,000개 구독자에게 여러 조각을 보내면 시간이 소요됩니다. 원격 성공과 로컬 상태 저장 사이 종료 시 중복 가능성은 남습니다.

[Telegram getUpdates](https://core.telegram.org/bots/api#getupdates)를 사용하므로 같은 봇 토큰의 다른 polling 프로세스나 webhook과 함께 실행하면 안 됩니다. `--once`는 RSS 한 주기 시험용이며 구독 명령과 대기 전송을 지속 처리하려면 기본 상시 실행 모드를 사용하세요. 새 기능은 토큰 없이 모의 API 테스트로도 검증합니다.
