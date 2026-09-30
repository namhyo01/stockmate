# AWS(t3a.micro)로 옮기기

t3a.micro는 메모리가 1GB라 스왑을 먼저 잡는다. 아래는 Ubuntu 24.04, 사용자 `ubuntu`, 설치 경로 `~/stockmate` 기준이다.

## 0-1. EC2 인스턴스와 예산

- 리전: 서울(ap-northeast-2)
- AMI: **Ubuntu Server 24.04 LTS, 64비트(x86)** (t3a는 AMD x86이라 Arm AMI로는 선택할 수 없다)
- 유형: t3a.micro, 스토리지: **gp3 20GiB**, 고급 → 크레딧 사양: **Standard** (Unlimited는 CPU 초과 사용분이 과금된다)
- 키 페어: ED25519 `.pem`을 `~/.ssh/`에 두고 `chmod 400` (WSL에서는 /mnt/c 아래에 두면 권한이 적용되지 않는다)
- 보안 그룹 `stockmate-sg`: 인바운드는 **SSH 22 / 내 IP 하나만**, HTTP·HTTPS·8000은 열지 않는다. Tailscale 설정 후에는 22번도 지운다.
- 예상 비용(월, 대략): 인스턴스 약 $8.5 + EBS 약 $1.8 + 퍼블릭 IPv4 약 $3.6 ≈ **$14**
- Budgets: 월 $20 비용 예산, 알림은 실제 50%·80%와 예측 100%, **실제 100%에서 EC2 인스턴스 중지 작업**. 비용 데이터는 몇 시간 늦게 반영된다.

## 0-2. 로컬에서 패키지 만들기

```bash
cd ~/work/ai/project/stock
bash scripts/pack.sh
scp -i <키.pem> ../stockmate-handoff-*.tar.gz ubuntu@<서버 IP>:~/
rm ../stockmate-handoff-*.tar.gz    # .env가 들어 있으니 옮긴 뒤 지운다
```

EC2 보안 그룹은 **SSH(22)만, 내 IP에서만** 열어둔다. 대시보드는 아래 3단계의 Tailscale로 접속한다.

## 1. 서버 기본 설정

```bash
sudo timedatectl set-timezone Asia/Seoul          # 코드가 한국 시간 기준이다

# 스왑 2GB (pandas 설치와 claude -p 실행 때 메모리가 모자라지 않게)
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# 압축 풀기
mkdir -p ~/stockmate && tar -xzf ~/stockmate-handoff-*.tar.gz -C ~/stockmate
chmod 600 ~/stockmate/.env && rm ~/stockmate-handoff-*.tar.gz

# Python 환경
curl -LsSf https://astral.sh/uv/install.sh | sh && source ~/.bashrc
cd ~/stockmate && uv sync && uv run pytest
uv run stock summary        # 한투 키로 시세가 나오면 성공
```

## 2. Claude Code (구독 계정)

```bash
curl -fsSL https://claude.ai/install.sh | bash
cd ~/stockmate && claude     # 처음 실행하면 로그인 URL이 나온다. 내 PC 브라우저에서 열어 로그인한다.
```

웹의 AI 분석과 질문하기는 서비스(systemd)가 `claude -p`를 실행한다. 로그인만으로 인증되지 않으면 장기 토큰을 만들어 `.env`에 넣는다.

```bash
claude setup-token           # 출력된 토큰을 복사
echo 'CLAUDE_CODE_OAUTH_TOKEN=<토큰>' >> ~/stockmate/.env
```

**로컬 대화 이어가기**: 서버의 Claude는 `CLAUDE.md`와 `docs/HANDOFF.md`를 읽고 맥락을 잡는다. 로컬 Claude의 메모리도 옮기려면 아래를 실행한다.

```bash
PROJ=~/.claude/projects/$(echo ~/stockmate | sed 's#/#-#g')
mkdir -p "$PROJ" && cp -r ~/stockmate/handoff/claude-memory "$PROJ/memory"
```

`handoff/session.jsonl`은 대화 원본이다. 필요할 때 Claude에게 "handoff/session.jsonl에서 ○○ 찾아줘"라고 하면 된다.

## 3. 대시보드 접속: Tailscale (추천)

인터넷에 포트를 열지 않고, 내 PC와 폰에서만 HTTPS로 접속할 수 있다.

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up                      # 로그인 URL을 브라우저에서 연다
sudo tailscale serve --bg 8000         # https://<서버이름>.<tailnet>.ts.net 으로 대시보드 공개 (내 기기에서만)
```

PC와 폰에도 Tailscale 앱을 설치하고 같은 계정으로 로그인한다. 만약을 위해 `.env`에 `WEB_PASSWORD=<비밀번호>`도 넣어둔다.

> 도메인으로 인터넷에 공개하고 싶다면 Caddy로 HTTPS를 붙이고 `WEB_PASSWORD`를 반드시 설정한다. 계좌 정보가 보이는 화면이라 HTTP로 공개하면 안 된다.

## 4. 서비스로 등록

```bash
sudo cp ~/stockmate/deploy/stockmate.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now stockmate
systemctl status stockmate
journalctl -u stockmate -f       # 로그 보기
```

코드를 고친 뒤에는 `sudo systemctl restart stockmate`.

## 5. 백업

`~/stockmate/data/`(DB, 기억, 리포트)와 `.env`만 있으면 어디서든 복구할 수 있다. 가끔 로컬로 받아둔다.

```bash
# 로컬 PC에서
scp -i <키.pem> -r ubuntu@<서버 IP>:~/stockmate/data ./stockmate-data-backup
```

## 확인 목록

- [ ] `timedatectl`이 Asia/Seoul
- [ ] `uv run pytest` 통과
- [ ] `uv run stock summary`에 29종목 시세가 나온다
- [ ] Tailscale 주소로 대시보드가 열린다
- [ ] 대시보드에서 "새로고침" 후 한투 동기화 메시지가 나온다
- [ ] "Claude에게 묻기"에 답이 온다 (claude 인증 확인)
