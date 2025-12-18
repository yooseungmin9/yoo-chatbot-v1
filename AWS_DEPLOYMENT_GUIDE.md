# AWS 배포 가이드 - www.yooseungmin.com

## 목차
1. [배포 아키텍처](#1-배포-아키텍처)
2. [사전 준비사항](#2-사전-준비사항)
3. [EC2 인스턴스 설정](#3-ec2-인스턴스-설정)
4. [애플리케이션 배포](#4-애플리케이션-배포)
5. [Nginx 리버스 프록시 설정](#5-nginx-리버스-프록시-설정)
6. [도메인 및 SSL 설정](#6-도메인-및-ssl-설정)
7. [서비스 관리](#7-서비스-관리)
8. [모니터링 및 유지보수](#8-모니터링-및-유지보수)
9. [트러블슈팅](#9-트러블슈팅)

---

## 1. 배포 아키텍처

```
사용자 (www.yooseungmin.com)
         ↓
    가비아 DNS → EC2 Public IP
         ↓
    Nginx (리버스 프록시)
    ├── HTTPS (443) - Let's Encrypt SSL
    └── HTTP (80) → HTTPS 리다이렉트
         ↓
    EC2 Instance (Ubuntu)
    ├── FastAPI Chatbot (Port 8002)
    ├── Ollama (Gemma 2 9B)
    └── Spring Boot (Port 8081)
         ↓
외부 서비스
├── MongoDB Atlas
├── Google Cloud TTS
├── CLOVA STT
├── ECOS/FRED API
└── yFinance/PyKRX
```

**핵심 구성 요약:**
| 항목 | 설정 |
|------|------|
| 인프라 | AWS EC2 Ubuntu |
| 도메인 | 가비아 구매 → DNS 연결 |
| 웹서버 | Nginx (리버스 프록시) |
| SSL | Let's Encrypt (Certbot) |
| 배포 | Git pull + 서비스 재시작 |

---

## 2. 사전 준비사항

### 2.1 AWS 계정 및 권한
- AWS 계정 생성
- IAM 사용자 생성 및 EC2FullAccess 권한 부여

### 2.2 도메인 준비 (가비아)
1. 가비아(https://www.gabia.com)에서 도메인 구매
2. DNS 관리 페이지 접근 준비

### 2.3 로컬 환경 확인
```bash
# Git 저장소 확인
cd /Users/yoo/chatbot-v1

# 환경변수 파일 확인
cat fastapi/chatbot/.env
```

---

## 3. EC2 인스턴스 설정

### 3.1 인스턴스 생성

**추천 인스턴스 타입:**
| 용도 | 타입 | 스펙 | 비용 |
|------|------|------|------|
| 개발/테스트 | t3.xlarge | 4 vCPU, 16GB RAM | ~$0.17/시간 |
| 프로덕션 (GPU) | g4dn.xlarge | 4 vCPU, 16GB RAM, GPU | ~$0.53/시간 |
| 경량 테스트 | t3.large | 2 vCPU, 8GB RAM | ~$0.08/시간 |

**생성 단계:**

1. **AWS Console → EC2 → Launch Instance**

2. **기본 설정:**
   ```
   Name: chatbot-server
   AMI: Ubuntu Server 22.04 LTS
   Instance Type: t3.xlarge
   ```

3. **키 페어 생성:**
   ```bash
   # 키 페어 다운로드 후 권한 설정
   chmod 400 ~/Downloads/chatbot-key.pem
   ```

4. **Security Group 설정:**
   ```
   Name: chatbot-sg

   Inbound Rules:
   - SSH (22): My IP
   - HTTP (80): 0.0.0.0/0
   - HTTPS (443): 0.0.0.0/0
   ```

5. **스토리지:**
   - Root Volume: 50GB gp3 (SSD)

### 3.2 EC2 초기 설정

```bash
# EC2 접속
ssh -i ~/Downloads/chatbot-key.pem ubuntu@<EC2_PUBLIC_IP>

# 시스템 업데이트
sudo apt-get update && sudo apt-get upgrade -y

# 필수 패키지 설치
sudo apt-get install -y git curl wget nginx

# Docker 설치
sudo apt-get install -y docker.io docker-compose
sudo usermod -aG docker ubuntu
newgrp docker

# Docker 서비스 시작
sudo systemctl enable docker
sudo systemctl start docker

# 설치 확인
docker --version
nginx -v
```

---

## 4. 애플리케이션 배포

### 4.1 코드 클론

```bash
# EC2에서 실행
cd ~
git clone https://github.com/YOUR_USERNAME/chatbot-v1.git
cd chatbot-v1/fastapi/chatbot
```

### 4.2 환경변수 설정

```bash
# .env 파일 생성
nano .env
```

`.env` 파일 내용:
```env
OPENAI_API_KEY=your_openai_api_key
GOOGLE_APPLICATION_CREDENTIALS=/app/key/your-google-key.json
FRED_API_KEY=your_fred_api_key
ECOS_API_KEY=your_ecos_api_key
CLOVA_KEY_ID=your_clova_key_id
CLOVA_KEY=your_clova_key
FFMPEG_BIN=/usr/bin/ffmpeg
```

### 4.3 Google Cloud 키 파일 업로드

```bash
# 로컬 Mac에서 실행
scp -i ~/Downloads/chatbot-key.pem -r \
    /Users/yoo/chatbot-v1/fastapi/chatbot/key \
    ubuntu@<EC2_PUBLIC_IP>:~/chatbot-v1/fastapi/chatbot/
```

### 4.4 Docker 빌드 및 실행

```bash
# EC2에서 실행
cd ~/chatbot-v1/fastapi/chatbot

# Docker 이미지 빌드
docker-compose build

# 컨테이너 실행 (백그라운드)
docker-compose up -d

# 상태 확인
docker-compose ps
docker-compose logs -f
```

### 4.5 서비스 동작 확인

```bash
# Health check
curl http://localhost:8002/health

# Ollama 모델 확인
docker exec chatbot-fastapi ollama list
```

---

## 5. Nginx 리버스 프록시 설정

### 5.1 Nginx 설정 파일 생성

```bash
sudo nano /etc/nginx/sites-available/chatbot
```

**설정 내용:**
```nginx
# HTTP → HTTPS 리다이렉트
server {
    listen 80;
    server_name yooseungmin.com www.yooseungmin.com;

    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    location / {
        return 301 https://$server_name$request_uri;
    }
}

# HTTPS 서버
server {
    listen 443 ssl;
    server_name yooseungmin.com www.yooseungmin.com;

    # SSL 인증서 (Let's Encrypt - 나중에 Certbot이 자동 설정)
    ssl_certificate /etc/letsencrypt/live/yooseungmin.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/yooseungmin.com/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    # 기본 페이지 (Spring Boot)
    location / {
        proxy_pass http://127.0.0.1:8081;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_cache_bypass $http_upgrade;
    }

    # FastAPI 챗봇 API
    location /api/ {
        proxy_pass http://127.0.0.1:8002/api/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 180s;
        proxy_connect_timeout 10s;
    }

    # Health check
    location /health {
        proxy_pass http://127.0.0.1:8002/health;
    }
}
```

### 5.2 설정 활성화

```bash
# 심볼릭 링크 생성
sudo ln -s /etc/nginx/sites-available/chatbot /etc/nginx/sites-enabled/

# 기본 설정 제거
sudo rm /etc/nginx/sites-enabled/default

# 설정 문법 검사
sudo nginx -t

# Nginx 재시작
sudo systemctl restart nginx
sudo systemctl enable nginx
```

---

## 6. 도메인 및 SSL 설정

### 6.1 가비아 DNS 설정

1. **가비아 로그인 → My가비아 → 도메인 관리**

2. **DNS 레코드 추가:**

   | 타입 | 호스트 | 값 | TTL |
   |------|--------|-----|-----|
   | A | @ | EC2_PUBLIC_IP | 300 |
   | A | www | EC2_PUBLIC_IP | 300 |

3. **DNS 전파 확인 (5~30분 소요):**
   ```bash
   # 로컬 Mac에서 실행
   nslookup yooseungmin.com
   nslookup www.yooseungmin.com
   ```

### 6.2 Let's Encrypt SSL 인증서 발급

```bash
# EC2에서 실행

# Certbot 설치
sudo apt-get install -y certbot python3-certbot-nginx

# SSL 인증서 발급 (자동으로 Nginx 설정도 수정)
sudo certbot --nginx -d yooseungmin.com -d www.yooseungmin.com
```

**Certbot 프롬프트:**
- Email: 본인 이메일 입력
- Terms of Service: Y
- Share email: N (선택)
- Redirect HTTP to HTTPS: 2 (Redirect)

### 6.3 SSL 인증서 자동 갱신 확인

```bash
# 갱신 테스트 (dry-run)
sudo certbot renew --dry-run

# Cron 자동 갱신 확인 (기본 설정됨)
sudo systemctl status certbot.timer
```

### 6.4 SSL 연결 테스트

```bash
# 로컬 Mac에서 실행
curl https://www.yooseungmin.com/health

# SSL 인증서 정보 확인
openssl s_client -connect www.yooseungmin.com:443 -servername www.yooseungmin.com
```

---

## 7. 서비스 관리

### 7.1 배포 스크립트 생성

```bash
# EC2에서 실행
nano ~/deploy.sh
```

**deploy.sh 내용:**
```bash
#!/bin/bash
# deploy.sh - Git pull 후 서비스 재시작

set -e

echo "=========================================="
echo "배포 시작: $(date)"
echo "=========================================="

cd ~/chatbot-v1

# 1. 최신 코드 가져오기
echo "→ Git pull..."
git pull origin master

# 2. FastAPI 컨테이너 재시작
echo "→ FastAPI 재시작..."
cd fastapi/chatbot
docker-compose down
docker-compose up -d --build

# 3. Spring Boot 재시작 (필요시)
# sudo systemctl restart spring-boot

# 4. 상태 확인
echo "→ 상태 확인..."
sleep 5
docker-compose ps
curl -s http://localhost:8002/health

echo "=========================================="
echo "배포 완료: $(date)"
echo "=========================================="
```

```bash
chmod +x ~/deploy.sh
```

### 7.2 서비스 상태 확인

```bash
# Docker 컨테이너 상태
docker-compose ps

# Nginx 상태
sudo systemctl status nginx

# 포트 확인
sudo netstat -tlnp | grep -E '80|443|8002|8081'

# 로그 확인
docker-compose logs -f --tail=100
sudo tail -f /var/log/nginx/error.log
```

### 7.3 서비스 재시작

```bash
# 전체 재배포
~/deploy.sh

# FastAPI만 재시작
cd ~/chatbot-v1/fastapi/chatbot
docker-compose restart

# Nginx만 재시작
sudo systemctl restart nginx
```

---

## 8. 모니터링 및 유지보수

### 8.1 백업 스크립트

```bash
nano ~/backup.sh
```

**backup.sh 내용:**
```bash
#!/bin/bash
# backup.sh - 데이터 백업

BACKUP_DIR="/home/ubuntu/backups"
DATE=$(date +%Y%m%d_%H%M%S)

mkdir -p $BACKUP_DIR

cd ~/chatbot-v1/fastapi/chatbot

# Vector store 백업
tar -czf $BACKUP_DIR/vectorstore_$DATE.tar.gz vectorstore/
tar -czf $BACKUP_DIR/docs_$DATE.tar.gz docs/

# 오래된 백업 삭제 (30일 이상)
find $BACKUP_DIR -name "*.tar.gz" -mtime +30 -delete

echo "Backup completed: $DATE"
```

```bash
chmod +x ~/backup.sh

# Cron 설정 (매일 새벽 3시)
crontab -e
# 추가: 0 3 * * * /home/ubuntu/backup.sh >> /home/ubuntu/backup.log 2>&1
```

### 8.2 모니터링 명령어

```bash
# 시스템 리소스
htop
df -h
free -h

# Docker 리소스
docker stats

# 네트워크 테스트
curl https://www.yooseungmin.com/health
```

### 8.3 로그 모니터링

```bash
# FastAPI 로그
docker-compose logs -f

# Nginx 접근 로그
sudo tail -f /var/log/nginx/access.log

# Nginx 에러 로그
sudo tail -f /var/log/nginx/error.log

# 시스템 로그
sudo journalctl -u nginx -f
```

---

## 9. 트러블슈팅

### 9.1 Nginx 502 Bad Gateway

**원인:** FastAPI 서버가 응답하지 않음

```bash
# FastAPI 상태 확인
docker-compose ps
curl http://localhost:8002/health

# 컨테이너 재시작
docker-compose restart

# 로그 확인
docker-compose logs -f
```

### 9.2 SSL 인증서 만료

```bash
# 인증서 상태 확인
sudo certbot certificates

# 수동 갱신
sudo certbot renew

# Nginx 재시작
sudo systemctl restart nginx
```

### 9.3 Ollama 메모리 부족

```bash
# Swap 메모리 추가 (8GB)
sudo fallocate -l 8G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile

# 영구 설정
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

### 9.4 DNS 전파 확인

```bash
# DNS 조회
nslookup yooseungmin.com
dig yooseungmin.com

# 전세계 DNS 전파 확인
# https://www.whatsmydns.net/
```

### 9.5 포트 충돌

```bash
# 포트 사용 확인
sudo lsof -i :8002
sudo lsof -i :8081
sudo lsof -i :80
sudo lsof -i :443

# 프로세스 종료
sudo kill -9 <PID>
```

### 9.6 MongoDB 연결 오류

```bash
# EC2 Public IP 확인
curl http://checkip.amazonaws.com

# MongoDB Atlas Network Access에 EC2 IP 추가
# https://cloud.mongodb.com → Network Access → Add IP Address
```

---

## 부록: 유용한 명령어 모음

```bash
# ===== 배포 =====
~/deploy.sh                           # 전체 재배포

# ===== Docker =====
docker-compose up -d                  # 백그라운드 실행
docker-compose down                   # 컨테이너 중지
docker-compose logs -f                # 실시간 로그
docker-compose restart                # 재시작
docker system prune -a                # 정리

# ===== Nginx =====
sudo nginx -t                         # 설정 검사
sudo systemctl restart nginx          # 재시작
sudo systemctl status nginx           # 상태 확인

# ===== SSL =====
sudo certbot certificates             # 인증서 상태
sudo certbot renew                    # 인증서 갱신
sudo certbot renew --dry-run          # 갱신 테스트

# ===== 모니터링 =====
htop                                  # CPU/메모리
df -h                                 # 디스크
docker stats                          # 컨테이너 리소스

# ===== 네트워크 =====
curl https://www.yooseungmin.com/health
sudo netstat -tlnp | grep -E '80|443|8002'
```

---

## 연락처

- **프로젝트 관리자**: 유승민
- **도메인**: www.yooseungmin.com
- **GitHub**: [chatbot-v1](https://github.com/YOUR_USERNAME/chatbot-v1)

---

**Last Updated**: 2025-12-18
