# 내부망 서버 배포 절차

개발은 Windows 로컬, 운영은 내부망 Ubuntu 서버를 전제로 한다.
사용자는 브라우저로만 접속하므로 서버 한 대에만 설치하면 된다.

## 무엇을 옮기는가

| 대상 | 방법 | 비고 |
|---|---|---|
| 코드 (`src/`, `app.py`, `eval/`) | git 또는 파일 복사 | 용량 작음 |
| 설정 (`config.yaml`) | 서버용으로 새로 작성 | 경로가 다르다 |
| 원본 문서 | **옮기지 않는다** | 이미 서버에 있다 |
| 인덱스 (`storage/`) | 서버에서 재생성 권장 | 복사도 가능(약 110MB) |
| conda 환경 | `environment.yml`로 재생성 | 파일만 옮기면 된다 |
| Ollama + 모델 | 서버에 새로 설치 | 약 4.8GB 다운로드 |

인덱스를 복사하지 않고 재생성하는 쪽을 권한다. GPU가 있으면 몇 분이면 끝나고,
Windows에서 만든 경로 메타데이터가 섞이지 않는다.

## 1. 코드 옮기기

원격 저장소가 없으므로 두 가지 중 편한 쪽을 쓴다.

**A. 서버에 bare 저장소를 두고 push** (이후 갱신이 편하다)

```bash
# 서버에서
git init --bare ~/repos/project-archive-rag.git
```

```bash
# 로컬에서
git remote add server ssh://<user>@<서버IP>/home/<user>/repos/project-archive-rag.git
git push server master
```

```bash
# 서버에서 작업 디렉터리로 clone
git clone ~/repos/project-archive-rag.git ~/project-archive-rag
```

이후 로컬에서 `git push server master`, 서버에서 `git pull`만 하면 갱신된다.

**B. 파일 복사** (한 번만 옮길 때)

```bash
scp -r src app.py eval environment.yml config.example.yaml .streamlit <user>@<서버IP>:~/project-archive-rag/
```

데이터·인덱스는 `.gitignore` 대상이므로 어느 방법이든 따라가지 않는다. 의도된 동작이다.

## 2. 환경 구축

```bash
cd ~/project-archive-rag
conda env create -f environment.yml
conda activate project-archive-rag
```

CUDA 버전이 로컬(12.6)과 다르면 torch만 다시 설치한다.

```bash
nvidia-smi | grep "CUDA Version"
```

```bash
pip install --force-reinstall torch --index-url https://download.pytorch.org/whl/cu121
```

GPU가 없으면 그냥 `pip install torch`로 CPU 빌드를 쓴다. 임베딩이 느려질 뿐 동작한다.

## 3. 설정 파일

```bash
cp config.example.yaml config.yaml
```

서버의 실제 경로로 고친다. Windows와 달리 슬래시를 쓴다.

```yaml
paths:
  sources:
    - "/data/과제자료/성과확인자료"
    - "/data/과제자료/회의록"
    - "/data/과제자료/과년도 보고서"
  storage: "/home/<user>/project-archive-rag/storage"

llm:
  provider: "ollama"
  base_url: "http://localhost:11434"
  model: "exaone3.5:7.8b"
```

원본 문서가 파일 서버에 있다면 읽기 전용으로 마운트하는 것이 안전하다.

## 4. Ollama 설치

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

```bash
ollama pull exaone3.5:7.8b
```

설치 스크립트가 systemd 서비스를 등록하므로 재부팅 후에도 자동 실행된다.

```bash
systemctl status ollama
```

## 5. 인덱스 생성

```bash
python src/ingest.py -s      # 파싱 → 청킹
python src/index.py          # 임베딩 + BM25
python src/perf_table.py     # 성과 표 → SQLite
```

문서 239개 기준으로 GPU면 5분 내외, CPU면 20~30분 걸린다.
잘 됐는지 확인:

```bash
python eval/run_eval.py
```

Hit@8이 로컬과 비슷하게(88% 안팎) 나오면 정상이다.

## 6-A. 필요할 때만 켜기 (권장 — 가끔 쓰는 경우)

가끔 조회하는 용도라면 상시 서비스로 둘 이유가 없다. SSH로 들어가 스크립트 하나로
켜고 끈다. `nohup`으로 띄우므로 **SSH를 끊어도 계속 돌아간다.**

```bash
cd ~/project-archive-rag
./run_server.sh start     # 띄운다 (접속 주소를 알려준다)
./run_server.sh status    # 상태 확인
./run_server.sh stop      # 내린다
./run_server.sh log       # 기동 실패 시 로그 확인
```

한 명이 `start` 하면 나머지 사람은 브라우저로만 접속한다. 각자 실행할 필요가 없다.

메모리 관점에서도 부담이 적다. 앱을 띄운 직후에는 임베딩 모델을 올리지 않고
**첫 검색 때** 올리며(약 2.5GB), 한 시간 동안 아무도 쓰지 않으면 캐시가 풀려
메모리를 돌려준다. Ollama도 요청이 없으면 5분 뒤 모델을 VRAM에서 내린다.
따라서 켜둔 채로 두어도 유휴 시 점유는 크지 않다.

## 6-B. 상시 실행 (systemd)

여러 사람이 자주 쓰고, 재부팅 후에도 자동으로 살아나야 할 때만 등록한다.

`/etc/systemd/system/project-archive-rag.service`:

```ini
[Unit]
Description=Project Archive RAG Streamlit UI
After=network.target ollama.service

[Service]
Type=simple
User=<user>
WorkingDirectory=/home/<user>/project-archive-rag
ExecStart=/home/<user>/miniconda3/envs/project-archive-rag/bin/streamlit run app.py \
    --server.address 0.0.0.0 --server.port 8501 --server.headless true
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now project-archive-rag
sudo systemctl status project-archive-rag
```

`conda activate` 대신 환경의 python 절대경로를 직접 쓰는 점에 유의한다.
systemd는 셸 초기화를 거치지 않는다.

## 7. 접속 허용

내부망에서만 열어야 한다. 공인 IP에 노출하지 않는다.

```bash
sudo ufw allow from 10.0.0.0/8 to any port 8501
```

대역은 기관 내부망에 맞게 바꾼다. 사용자는 `http://<서버IP>:8501`로 접속한다.

## 8. 간단한 접근 제어 (선택)

내부망이라도 최소한의 인증을 두려면 `.streamlit/secrets.toml`에 비밀번호를 두고
`app.py` 상단에서 확인하는 방식이 가장 간단하다. 더 엄격하게 하려면 nginx를 앞에
두고 basic auth를 걸거나 기관 SSO에 연동한다.

## 갱신할 때

문서가 추가·수정되면:

```bash
cd ~/project-archive-rag && git pull        # 코드 변경이 있을 때
python src/ingest.py -s && python src/index.py && python src/perf_table.py
sudo systemctl restart project-archive-rag
```

인덱스를 다시 만드는 동안에도 기존 UI는 동작한다(Chroma 컬렉션이 교체되는
순간만 짧게 영향을 받는다). 무중단이 필요하면 인덱스를 새 폴더에 만든 뒤
`config.yaml`의 `storage`를 바꾸고 재시작한다.

## 점검 항목

- [ ] 서버에서 원본 문서 폴더를 읽을 수 있는가 (권한, 마운트)
- [ ] `nvidia-smi`로 GPU가 보이는가 (없으면 CPU로도 동작)
- [ ] `python eval/run_eval.py` 결과가 로컬과 비슷한가
- [ ] Ollama 응답 속도 (`ollama run exaone3.5:7.8b "안녕" --verbose`, 35 tok/s 이상)
- [ ] 재부팅 후 자동 기동되는가 (`sudo reboot` 후 확인)
- [ ] 외부에서 접근되지 않는가 (내부망 전용 확인)
