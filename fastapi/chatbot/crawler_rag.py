# crawler_rag.py — 오늘 기사 수집
# 실행(간단 테스트): python crawler_rag.py  → 10개 수집
# 스케줄러: from crawler_rag import crawl_today

import re
import time
import logging
from typing import Dict, List
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from pymongo import MongoClient, ASCENDING
from dateutil import parser


# ===== MongoDB 연결/컬렉션 준비 =====
# 고정 접속정보/DB/컬렉션; url 유니크 인덱스로 중복 방지
MONGO_URI = "mongodb+srv://Dgict_TeamB:team1234@cluster0.5d0uual.mongodb.net/"
DB_NAME   = "test123"
COLL_NAME = "chatbot_rag"

def get_collection():
    """Mongo 컬렉션 반환 + url 유니크 인덱스 보장"""
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
    col = client[DB_NAME][COLL_NAME]
    col.create_index([("url", ASCENDING)], name="uniq_url", unique=True)
    return col

# ===== 네이버 목록/파싱 설정 =====
# 최소 UA/Referer 지정, 경제섹션 리스트 URL 생성/파싱
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://news.naver.com/"}
BASE_LIST = "https://news.naver.com/main/list.naver"

# 언론사 OID → 이름 매핑 (필요 최소만)
OIDS: Dict[str, str] = {
    "056":"KBS","015":"한국경제","009":"매일경제","014":"파이낸셜뉴스","119":"데일리안","005":"국민일보",
    "421":"뉴스1","047":"오마이뉴스","001":"연합뉴스","629":"더팩트","029":"디지털타임스","008":"머니투데이",
    "028":"한겨레","448":"TV조선","023":"조선일보","082":"부산일보","277":"아시아경제","422":"연합뉴스TV",
    "018":"이데일리","092":"지디넷코리아","052":"YTN","020":"동아일보","055":"SBS","003":"뉴시스","469":"한국일보",
    "366":"조선비즈","025":"중앙일보","079":"노컷뉴스","659":"전주MBC","437":"JTBC","016":"헤럴드경제","032":"경향신문",
    "214":"MBC","215":"한국경제TV","138":"디지털데일리","011":"서울경제","586":"시사저널","044":"코리아헤럴드",
    "002":"프레시안","021":"문화일보","087":"강원일보","081":"서울신문","666":"경기일보","088":"매일신문","057":"MBN",
    "449":"채널A","022":"세계일보","374":"SBS Biz","030":"전자신문","346":"헬스조선","037":"주간동아","656":"대전일보",
    "031":"아이뉴스24","648":"비즈워치","660":"kbc광주방송","640":"코리아중앙데일리","654":"강원도민일보","607":"뉴스타파",
    "661":"JIBS","006":"미디어오늘","310":"여성신문","262":"신동아","094":"월간 산","308":"시사IN","024":"매경이코노미",
    "293":"블로터","123":"조세일보","657":"대구MBC","662":"농민신문","243":"이코노미스트","417":"머니S","036":"한겨레21",
    "584":"동아사이언스","007":"일다","050":"한경비즈니스","655":"CJB청주방송","033":"주간경향","296":"코메디닷컴",
    "053":"주간조선","127":"기자협회보","658":"국제신문","665":"더스쿠프","353":"중앙SUNDAY","145":"레이디경향"
}

def build_url(date: str, page: int) -> str:
    """경제 섹션(sid1=101)에서 날짜/페이지 기준 목록 URL 생성"""
    return f"{BASE_LIST}?mode=LSD&mid=sec&sid1=101&date={date}&page={page}"

def extract_links(html: str) -> List[tuple]:
    """목록 HTML에서 기사 제목/링크 추출"""
    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.select("ul.type06_headline li dt a, ul.type06 li dt a")
    return [(a.get_text(strip=True), a.get("href","")) for a in anchors if "/article/" in a.get("href","")]

def fetch_article(link: str) -> Dict[str, str]:
    """기사 상세 페이지에서 제목/본문/이미지/언론사/발행시각 파싱"""
    r = requests.get(link, headers=UA, timeout=10)
    r.raise_for_status()
    s = BeautifulSoup(r.text, "html.parser")

    # 제목(표준/구버전/OG 메타 대응)
    title_node = s.select_one("h2#title_area, h3#articleTitle, meta[property='og:title']")
    if title_node:
        title = title_node.get("content") if title_node.name == "meta" else title_node.get_text(strip=True)
    else:
        title = ""

    # 본문(신/구 DOM 대응)
    body_node = s.select_one("article#dic_area, div#articeBody, div#newsct_article")
    content = body_node.get_text(" ", strip=True) if body_node else ""

    # 대표이미지(og:image)
    og = s.select_one('meta[property="og:image"]')
    image_url = og.get("content") if og and og.get("content") else ""

    # 언론사 (URL OID 기반 추론)
    press = ""
    m = re.search(r"article/(\d{3})/", link)
    if m and m.group(1) in OIDS:
        press = OIDS[m.group(1)]

    # 발행시각(데이터 속성 → OG 메타 순)
    pub_time = None
    t1 = s.select_one("span.media_end_head_info_datestamp_time")
    if t1 and t1.get("data-date-time"):
        pub_time = parser.parse(t1["data-date-time"])
    else:
        t2 = s.select_one('meta[property="og:article:published_time"]')
        if t2 and t2.get("content"):
            pub_time = parser.parse(t2["content"])

    return {
        "title": title,
        "content": content,
        "image": image_url,
        "press": press,
        "published_at": pub_time
    }

# ===== 오늘만 수집 (KST 기준) =====
# 날짜=오늘, 목록 페이징 순회, 중복/타일라인 필터, 최대 N건 저장
def crawl_today(limit_per_run: int = 50):
    col = get_collection()
    KST = ZoneInfo("Asia/Seoul")
    today_str = datetime.now(KST).strftime("%Y%m%d")

    inserted, page = 0, 1
    logging.info("오늘 수집 시작: %s (최대 %d개)", today_str, limit_per_run)

    while inserted < limit_per_run:
        url = build_url(today_str, page)
        try:
            res = requests.get(url, headers=UA, timeout=10)
            res.raise_for_status()
        except Exception as e:
            logging.warning("[목록 실패] %s (%s)", url, e)
            break

        links = extract_links(res.text)
        if not links:
            logging.info("목록 끝(page=%d)", page)
            break

        for title, link in links:
            # 선조회로 중복 절약(인덱스는 최후 방어)
            if col.find_one({"url": link}):
                continue

            art = fetch_article(link)
            if not art["published_at"]:
                continue

            # 발행일=오늘(KST)만 저장
            pub_date_kst = art["published_at"].astimezone(KST).date()
            if pub_date_kst.strftime("%Y%m%d") != today_str:
                continue

            # 최소 필드 저장(추가 가공은 후처리 권장)
            doc = {
                "title": art["title"] or title,
                "url": link,
                "content": art["content"],
                "image": art["image"],
                "press": art["press"],
                "main_section": "경제",
                "published_at": art["published_at"].isoformat(),  # 원문 타임존 보존
                "collected_at": datetime.now(KST).isoformat(),    # 수집 시각(KST)
            }
            try:
                col.insert_one(doc)
                inserted += 1
                logging.info("[OK %d/%d] %s", inserted, limit_per_run, link)
            except Exception as e:
                logging.warning("[SKIP] 저장 실패: %s", e)

            if inserted >= limit_per_run:
                break

        page += 1
        time.sleep(0.3)

    logging.info("오늘 수집 종료: 총 %d개 저장", inserted)

# ===== 간단 테스트 =====
# 파이프라인 점검용: 10개만 수집
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    crawl_today(limit_per_run=10)