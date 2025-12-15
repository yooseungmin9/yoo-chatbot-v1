# AWS 배포 가이드 - www.yooseungmin.com

## 목차
1. [배포 아키텍처](#1-배포-아키텍처)
2. [사전 준비사항](#2-사전-준비사항)
3. [EC2 인스턴스 설정](#3-ec2-인스턴스-설정)
4. [애플리케이션 배포](#4-애플리케이션-배포)
5. [도메인 및 SSL 설정](#5-도메인-및-ssl-설정)
6. [로드밸런서 및 보안 설정](#6-로드밸런서-및-보안-설정)
7. [모니터링 및 유지보수](#7-모니터링-및-유지보수)

---

## 1. 배포 아키텍처

```
사용자 (www.yooseungmin.com)
         ↓
    Route 53 (DNS)
         ↓
Application Load Balancer (ALB) + SSL/TLS
         ↓
    EC2 Instance(s)
    ├── Docker (FastAPI Chatbot - Port 8002)
    ├── Ollama (Gemma 2 9B)
    └── Spring Boot (Port 8081)
         ↓
외부 서비스
├── MongoDB Atlas
├── OpenAI API
├── Google Cloud TTS
├── CLOVA STT
├── ECOS/FRED API
└── yFinance
```

---

## 2. 사전 준비사항

### 2.1 AWS 계정 및 권한
- AWS 계정 생성
- IAM 사용자 생성 및 다음 권한 부여:
  - EC2FullAccess
  - ElasticLoadBalancingFullAccess
  - Route53FullAccess
  - CertificateManagerFullAccess

### 2.2 도메인 설정
도메인 `www.yooseungmin.com`이 어디서 구입되었는지에 따라:

**Option B: 외부 도메인 등록업체 (Gabia)**
1. AWS Route 53에서 Hosted Zone 생성
2. Gabia에서 네임서버를 AWS Route 53의 NS 레코드로 변경

### 2.3 로컬 환경 확인
```bash
# Git 저장소 확인
cd /Users/yoo/chatbot-v1

# 환경변수 파일 확인
cat fastapi/chatbot/.env

# Docker 설치 확인
docker --version
docker-compose --version
```

---

## 3. EC2 인스턴스 설정

### 3.1 인스턴스 생성

**추천 인스턴스 타입:**
- **개발/테스트**: `t3.xlarge` (4 vCPU, 16GB RAM) - 약 $0.1664/시간
- **프로덕션**: `g4dn.xlarge` (4 vCPU, 16GB RAM, GPU) - 약 $0.526/시간 (Ollama 성능 최적화)
- **경량 테스트**: `t3.large` (2 vCPU, 8GB RAM) - 약 $0.0832/시간

**생성 단계:**

1. **AWS Console → EC2 → Launch Instance**

2. **기본 설정:**
   ```
   Name: chatbot-server
   AMI: Ubuntu Server 22.04 LTS
   Instance Type: t3.xlarge (또는 g4dn.xlarge)
   ```

3. **키 페어 (Key Pair):**
   - 새 키 페어 생성: `chatbot-key.pem`
   - 다운로드 후 안전한 위치에 저장
   ```bash
   chmod 400 ~/Downloads/chatbot-key.pem
   ```

4. **네트워크 설정:**
   - VPC: Default VPC 사용
   - Auto-assign Public IP: Enable
   - Security Group 생성:
     ```
     Name: chatbot-sg

     Inbound Rules:
     - SSH (22): My IP (현재 IP만 허용)
     - HTTP (80): Anywhere (0.0.0.0/0)
     - HTTPS (443): Anywhere (0.0.0.0/0)
     - Custom TCP (8002): ALB Security Group (나중에 추가)
     - Custom TCP (8081): ALB Security Group (나중에 추가)
     ```

5. **스토리지:**
   - Root Volume: 50GB gp3 (SSD)

6. **Launch Instance**

### 3.2 EC2 접속 및 초기 설정

```bash
# EC2 접속
ssh -i ~/Downloads/chatbot-key.pem ubuntu@<EC2_PUBLIC_IP>

# 시스템 업데이트
sudo apt-get update && sudo apt-get upgrade -y

# 필수 패키지 설치
sudo apt-get install -y git curl wget docker.io docker-compose

# Docker 권한 설정
sudo usermod -aG docker ubuntu
newgrp docker

# Docker 서비스 시작
sudo systemctl enable docker
sudo systemctl start docker

# 설치 확인
docker --version
docker-compose --version
```

---

## 4. 애플리케이션 배포

### 4.1 코드 업로드

**Option A: Git을 통한 배포**

```bash
# EC2에서 실행
cd ~
git clone https://github.com/YOUR_USERNAME/chatbot-v1.git
cd chatbot-v1/fastapi/chatbot
```

**Option B: SCP를 통한 직접 업로드**

```bash
# 로컬 Mac에서 실행
cd /Users/yoo/chatbot-v1
scp -i ~/Downloads/chatbot-key.pem -r fastapi/chatbot ubuntu@<EC2_PUBLIC_IP>:~/
```

### 4.2 환경변수 설정

```bash
# EC2에서 실행
cd ~/chatbot-v1/fastapi/chatbot

# .env 파일 생성 (로컬 .env 내용 복사)
nano .env
```

`.env` 파일 내용:
```env
OPENAI_API_KEY=your_openai_api_key
GOOGLE_APPLICATION_CREDENTIALS=/app/key/absolute-text-473306-c1-7db0108e1c22.json
FRED_API_KEY=your_fred_api_key
ECOS_API_KEY=your_ecos_api_key
CLOVA_KEY_ID=your_clova_key_id
CLOVA_KEY=your_clova_key
FFMPEG_BIN=/usr/bin/ffmpeg
```

```bash
# 로컬 Mac에서 실행
scp -i ~/Downloads/chatbot-key.pem -r /Users/yoo/chatbot-v1/fastapi/chatbot/key ubuntu@<EC2_PUBLIC_IP>:~/chatbot-v1/fastapi/chatbot/
```

### 4.3 Docker 빌드 및 실행

```bash
# EC2에서 실행
cd ~/chatbot-v1/fastapi/chatbot

# Docker 이미지 빌드
docker-compose build

# 컨테이너 실행 (백그라운드)
docker-compose up -d

# 로그 확인
docker-compose logs -f

# 컨테이너 상태 확인
docker-compose ps
```

### 4.4 서비스 동작 확인

```bash
# Health check
curl http://localhost:8002/health

# Ollama 모델 확인
docker exec chatbot-fastapi ollama list
```

---

## 5. 도메인 및 SSL 설정

### 5.1 Route 53 Hosted Zone 설정

1. **AWS Console → Route 53 → Hosted zones**

2. **Create hosted zone** (이미 있으면 스킵)
   ```
   Domain name: yooseungmin.com
   Type: Public hosted zone
   ```

3. **NS 레코드 확인**
   - Hosted Zone을 생성하면 4개의 NS 레코드가 자동 생성됨
   - 이 NS 레코드를 도메인 등록업체에 설정 (외부 등록업체 사용 시)

### 5.2 SSL/TLS 인증서 발급 (AWS Certificate Manager)

1. **AWS Console → Certificate Manager (ACM)**

2. **Request a certificate**
   ```
   Certificate type: Public certificate
   Domain names:
     - yooseungmin.com
     - www.yooseungmin.com
     - *.yooseungmin.com (와일드카드, 선택사항)

   Validation method: DNS validation (추천)
   ```

3. **DNS validation 레코드 추가**
   - ACM이 제공하는 CNAME 레코드를 Route 53에 추가
   - "Create records in Route 53" 버튼 클릭 (자동 추가)

4. **인증서 발급 대기**
   - 보통 5~30분 소요
   - Status가 "Issued"가 되면 완료

---

## 6. 로드밸런서 및 보안 설정

### 6.1 Application Load Balancer (ALB) 생성

1. **AWS Console → EC2 → Load Balancers → Create Load Balancer**

2. **Load balancer type: Application Load Balancer**

3. **기본 설정:**
   ```
   Name: chatbot-alb
   Scheme: Internet-facing
   IP address type: IPv4
   ```

4. **Network mapping:**
   - VPC: Default VPC
   - Availability Zones: 최소 2개 이상 선택
     - us-east-1a
     - us-east-1b

5. **Security groups:**
   - 새 Security Group 생성: `alb-sg`
     ```
     Inbound Rules:
     - HTTP (80): 0.0.0.0/0
     - HTTPS (443): 0.0.0.0/0
     ```

6. **Listeners and routing:**

   **HTTP Listener (Port 80):**
   - Protocol: HTTP
   - Port: 80
   - Action: Redirect to HTTPS (443)

   **HTTPS Listener (Port 443):**
   - Protocol: HTTPS
   - Port: 443
   - SSL certificate: ACM에서 발급받은 인증서 선택
   - Default action: Forward to Target Group (다음 단계에서 생성)

### 6.2 Target Group 생성

1. **Target type: Instances**

2. **기본 설정:**
   ```
   Name: chatbot-tg
   Protocol: HTTP
   Port: 8002
   VPC: Default VPC
   ```

3. **Health checks:**
   ```
   Protocol: HTTP
   Path: /health
   Healthy threshold: 2
   Unhealthy threshold: 3
   Timeout: 5 seconds
   Interval: 30 seconds
   ```

4. **Register targets:**
   - EC2 인스턴스 선택 (`chatbot-server`)
   - Port: 8002
   - Include as pending below

5. **Create target group**

6. **ALB의 HTTPS Listener에 Target Group 연결**
   - ALB → Listeners → HTTPS:443 → Edit rules
   - Forward to: `chatbot-tg`

### 6.3 Security Group 업데이트

**EC2 Security Group (`chatbot-sg`) 수정:**
```
Inbound Rules 추가:
- Custom TCP (8002): Source = alb-sg (ALB Security Group)
- Custom TCP (8081): Source = alb-sg (Spring Boot용, 필요시)

SSH (22) 규칙 수정:
- Source: My IP (보안 강화)
```

### 6.4 Route 53 레코드 생성

1. **AWS Console → Route 53 → Hosted zones → yooseungmin.com**

2. **A 레코드 생성 (www.yooseungmin.com):**
   ```
   Record name: www
   Record type: A
   Alias: Yes
   Route traffic to: Alias to Application Load Balancer
   Region: us-east-1 (ALB가 있는 리전)
   Load Balancer: chatbot-alb
   ```

3. **A 레코드 생성 (yooseungmin.com - root domain):**
   ```
   Record name: (비워둠)
   Record type: A
   Alias: Yes
   Route traffic to: Alias to Application Load Balancer
   Region: us-east-1
   Load Balancer: chatbot-alb
   ```

4. **DNS 전파 확인 (5~10분 소요):**
   ```bash
   # 로컬 Mac에서 실행
   nslookup www.yooseungmin.com
   curl https://www.yooseungmin.com/health
   ```

---

## 7. 모니터링 및 유지보수

### 7.1 CloudWatch 로그 설정

```bash
# EC2에 CloudWatch 에이전트 설치
wget https://s3.amazonaws.com/amazoncloudwatch-agent/ubuntu/amd64/latest/amazon-cloudwatch-agent.deb
sudo dpkg -i -E ./amazon-cloudwatch-agent.deb

# 로그 설정
sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
    -a fetch-config \
    -m ec2 \
    -s -c file:/opt/aws/amazon-cloudwatch-agent/etc/config.json
```

### 7.2 자동 백업 설정

**Docker Volume 백업 스크립트:**

```bash
# EC2에서 실행
nano ~/backup.sh
```

```bash
#!/bin/bash
# backup.sh

BACKUP_DIR="/home/ubuntu/backups"
DATE=$(date +%Y%m%d_%H%M%S)

mkdir -p $BACKUP_DIR

# Vector store 백업
cd ~/chatbot-v1/fastapi/chatbot
tar -czf $BACKUP_DIR/vectorstore_$DATE.tar.gz vectorstore/
tar -czf $BACKUP_DIR/docs_$DATE.tar.gz docs/

# 오래된 백업 삭제 (30일 이상)
find $BACKUP_DIR -name "*.tar.gz" -mtime +30 -delete

echo "Backup completed: $DATE"
```

```bash
chmod +x ~/backup.sh

# Cron 설정 (매일 새벽 3시 백업)
crontab -e
# 추가: 0 3 * * * /home/ubuntu/backup.sh >> /home/ubuntu/backup.log 2>&1
```

### 7.3 애플리케이션 업데이트

```bash
# EC2에서 실행
cd ~/chatbot-v1

# 최신 코드 가져오기
git pull origin master

# 컨테이너 재시작
cd fastapi/chatbot
docker-compose down
docker-compose build
docker-compose up -d

# 로그 확인
docker-compose logs -f
```

### 7.4 비용 최적화

**예상 월 비용 (us-east-1 기준):**
```
EC2 t3.xlarge (24시간 운영):     ~$120/월
ALB (처리량에 따라):              ~$16-30/월
Route 53 Hosted Zone:            $0.50/월
Data Transfer (1TB 기준):        ~$90/월
-------------------------------------------
총 예상 비용:                     ~$230-250/월
```

**비용 절감 방안:**
1. **Reserved Instance 구매** (1년 약정 시 40% 할인)
2. **Auto Scaling 설정** (트래픽이 적을 때 인스턴스 축소)
3. **CloudFront CDN 사용** (정적 파일 캐싱, Data Transfer 비용 절감)
4. **Spot Instance 사용** (개발/테스트 환경)

---

## 8. 트러블슈팅

### 8.1 Ollama 메모리 부족

```bash
# EC2에서 실행
# Swap 메모리 추가 (8GB)
sudo fallocate -l 8G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile

# 영구 설정
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

### 8.2 Docker 컨테이너가 시작되지 않음

```bash
# 로그 확인
docker-compose logs chatbot

# 컨테이너 재시작
docker-compose restart

# 완전 재빌드
docker-compose down
docker system prune -a -f
docker-compose build --no-cache
docker-compose up -d
```

### 8.3 SSL 인증서 오류

```bash
# ALB Listener 확인
# AWS Console → EC2 → Load Balancers → chatbot-alb → Listeners

# 인증서 갱신 (Let's Encrypt 사용 시)
# ACM 인증서는 자동 갱신됨
```

### 8.4 MongoDB 연결 오류

```bash
# MongoDB Atlas IP Whitelist 확인
# EC2 Public IP를 MongoDB Atlas Network Access에 추가

# EC2 Public IP 확인
curl http://checkip.amazonaws.com
```

---

## 9. 다음 단계 (선택사항)

### 9.1 CI/CD 파이프라인 구축
- GitHub Actions 또는 AWS CodePipeline 사용
- Git push 시 자동 배포

### 9.2 Multi-AZ 고가용성 구성
- EC2 Auto Scaling Group 생성
- 여러 가용영역에 인스턴스 분산

### 9.3 데이터베이스 마이그레이션
- MongoDB Atlas → AWS DocumentDB
- 더 나은 AWS 통합 및 관리

### 9.4 캐싱 레이어 추가
- ElastiCache (Redis) 사용
- API 응답 캐싱으로 성능 향상

---

## 10. 연락처 및 지원

- **프로젝트 관리자**: 유승민
- **GitHub Repository**: [chatbot-v1](https://github.com/YOUR_USERNAME/chatbot-v1)
- **AWS Support**: https://console.aws.amazon.com/support

---

## 부록: 유용한 명령어

```bash
# Docker 관련
docker-compose up -d              # 백그라운드 실행
docker-compose down               # 컨테이너 중지 및 삭제
docker-compose logs -f chatbot    # 실시간 로그 확인
docker-compose restart chatbot    # 특정 서비스 재시작
docker system prune -a            # 사용하지 않는 이미지/컨테이너 삭제

# 시스템 모니터링
htop                              # CPU/메모리 사용량
df -h                             # 디스크 사용량
free -h                           # 메모리 사용량
docker stats                      # 컨테이너별 리소스 사용량

# 네트워크 테스트
curl https://www.yooseungmin.com/health
telnet <EC2_PUBLIC_IP> 8002
netstat -tlnp | grep 8002

# Ollama 관리
docker exec chatbot-fastapi ollama list
docker exec chatbot-fastapi ollama ps
docker exec chatbot-fastapi ollama pull gemma2:9b
```
