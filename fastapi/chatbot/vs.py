# pip install --upgrade openai watchdog
# ↑ 프로그램 실행 전에 꼭 필요한 패키지를 설치하세요.
#   - openai : OpenAI API와 통신
#   - watchdog : 폴더/파일 변화를 실시간 감시

import os, time, json, threading, hashlib, shutil, sys
from pathlib import Path
from typing import Dict
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from openai import OpenAI

# ====== OpenAI API 키 ======
# 보통은 환경변수로 설정하지만, 여기서는 코드에 직접 넣어둔 상태
API_KEY= "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"   # 실제 서비스용 코드에서는 환경변수로 처리

# =========[ 기본 설정 ]=========
DOCS_DIR = Path("./docs")                 # 감시할 폴더 (여기에 파일 넣으면 자동 업로드)
STAGING_DIR = Path(".staging")            # 업로드 전 임시 복사하는 폴더 (잠금/충돌 회피용)
STATE_FILE = Path(".vs_state.json")       # 파일 상태 기록 (hash, file_id 저장)
VS_ID_FILE = Path(".vector_store_id")     # 생성된 벡터스토어 ID를 저장

# 이벤트 처리 정책
ONLY_MOVE_CREATE = True   # True면 "on_modified" 이벤트 무시 (최종 저장/이동만 반영)
DEBOUNCE_SECS = 1.5       # 여러 이벤트가 동시에 터지면 1.5초 기다렸다가 한 번만 실행
DWELL_SECS = 2.0          # 파일이 "완전히 저장 끝난 상태"인지 확인하기 위한 대기 시간

# 허용되는 파일 확장자 및 임시파일 패턴
ALLOW_EXTS = {".pdf", ".docx", ".pptx", ".xlsx", ".txt", ".md"}  # 허용 확장자
LOCK_PREFIXES = ("~$",)           # MS 오피스가 생성하는 잠금파일 접두사
LOCK_SUFFIXES = (".tmp", ".part", ".lock")  # 임시 확장자

# OpenAI 연결 설정
client = OpenAI(api_key = API_KEY)
VECTOR_STORE_NAME = "econ-news-spec-store"
# ===========================


# ---------- 상태 관리 ----------
def load_state() -> Dict:
    """이전에 어떤 파일을 올렸는지 기억하는 상태 파일을 불러옴"""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"vector_store_id": None, "files": {}}

def save_state(state: Dict):
    """현재 상태를 파일로 저장"""
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

state = load_state()

def ensure_vector_store() -> str:
    """벡터스토어가 있으면 재사용, 없으면 새로 생성"""
    if VS_ID_FILE.exists():
        vsid = VS_ID_FILE.read_text().strip()
        state["vector_store_id"] = vsid
        return vsid
    vs = client.vector_stores.create(name=VECTOR_STORE_NAME)
    vsid = vs.id
    VS_ID_FILE.write_text(vsid)
    state["vector_store_id"] = vsid
    save_state(state)
    print(f"[VS] Created vector store: {vsid}")
    return vsid

VS_ID = ensure_vector_store()
DOCS_DIR.mkdir(parents=True, exist_ok=True)   # 감시 폴더 없으면 새로 생성
STAGING_DIR.mkdir(exist_ok=True)              # 스테이징 폴더 없으면 새로 생성


# ---------- 유틸 함수 ----------
def is_lock_like(p: Path) -> bool:
    """임시/잠금 파일인지 판별"""
    name = p.name
    return name.startswith(LOCK_PREFIXES) or p.suffix.lower() in LOCK_SUFFIXES

def file_key(p: Path) -> str:
    """파일 경로를 통일된 문자열로 변환 (상태 저장용 key)"""
    return str(p.resolve()).lower()

def stable_hash(p: Path) -> str:
    """파일 내용을 해시값으로 변환 → 변경 여부 확인 용도"""
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):  # 1MB 단위로 읽음
            h.update(chunk)
    return h.hexdigest()

def is_stable(p: Path, dwell=DWELL_SECS) -> bool:
    """파일이 일정 시간 동안 변하지 않으면 '안정' 상태로 간주"""
    try:
        s1 = (p.stat().st_size, p.stat().st_mtime)
        time.sleep(dwell)
        s2 = (p.stat().st_size, p.stat().st_mtime)
        return s1 == s2
    except FileNotFoundError:
        return False

def safe_copy_to_staging(src: Path) -> Path:
    """원본을 직접 업로드하지 않고 .staging 폴더로 안전하게 복사"""
    dst = STAGING_DIR / f"{src.stem}__staging{src.suffix}"
    tries, delay = 0, 0.5
    while True:
        try:
            shutil.copy2(src, dst)
            return dst
        except Exception:
            tries += 1
            if tries > 10:
                raise
            time.sleep(delay)
            delay = min(4.0, delay * 1.7)


# ---------- 업로드/링크 ----------
def upload_and_link(path: Path):
    """파일을 벡터스토어에 업로드하거나 기존 파일을 갱신"""
    if not path.exists():
        return
    if path.suffix.lower() not in ALLOW_EXTS or is_lock_like(path):
        return

    # 저장 중인 파일이면 조금 더 기다렸다가 업로드
    if not is_stable(path, dwell=DWELL_SECS):
        if not is_stable(path, dwell=DWELL_SECS):
            schedule_upload(path)
            return

    key = file_key(path)
    new_hash = stable_hash(path)
    old = state["files"].get(key, {})

    # 해시가 같으면 내용이 안 바뀐 것이므로 스킵
    if old.get("hash") == new_hash:
        print(f"[SKIP] unchanged: {path.name}")
        return

    print(f"[UPLOAD] {path.name} (hash changed)")

    # 복사본 업로드
    try:
        staging = safe_copy_to_staging(path)
    except Exception as e:
        print(f"[WARN] copy failed (locked?): {e}. reschedule.")
        schedule_upload(path)
        return

    try:
        # OpenAI에 파일 업로드
        with staging.open("rb") as f:
            fobj = client.files.create(file=f, purpose="assistants")
        # 벡터스토어에 연결
        client.vector_stores.files.create(vector_store_id=VS_ID, file_id=fobj.id)

        # 이전 파일 삭제
        old_file_id = old.get("file_id")
        if old_file_id:
            try:
                client.vector_stores.files.delete(vector_store_id=VS_ID, file_id=old_file_id)
                print(f"[CLEANUP] removed old file_id={old_file_id}")
            except Exception as e:
                print(f"[WARN] delete old failed: {e}")

        # 상태 갱신
        state["files"][key] = {"file_id": fobj.id, "hash": new_hash, "name": path.name}
        save_state(state)
        print(f"[OK] linked {path.name} → file_id={fobj.id}")

    finally:
        try:
            staging.unlink(missing_ok=True)  # 스테이징 파일 삭제
        except Exception:
            pass

def remove_from_vector_store(path: Path):
    """파일이 삭제되면 벡터스토어에서도 삭제"""
    if path.suffix.lower() not in ALLOW_EXTS or is_lock_like(path):
        return
    key = file_key(path)
    meta = state["files"].get(key)
    if not meta:
        return
    fid = meta.get("file_id")
    try:
        client.vector_stores.files.delete(vector_store_id=VS_ID, file_id=fid)
        print(f"[DELETE] {path.name} (file_id={fid})")
    except Exception as e:
        print(f"[WARN] delete failed: {e}")
    state["files"].pop(key, None)
    save_state(state)


# ---------- 디바운스 ----------
_timers: Dict[str, threading.Timer] = {}

def schedule_upload(path: Path):
    """짧은 시간 내 여러 번 이벤트가 발생하면 합쳐서 한번만 실행"""
    key = file_key(path)
    t = _timers.get(key)
    if t:
        t.cancel()
    timer = threading.Timer(DEBOUNCE_SECS, upload_and_link, args=[path])
    _timers[key] = timer
    timer.start()


# ---------- 이벤트 핸들러 ----------
class DocEventHandler(FileSystemEventHandler):
    """폴더 감시 중 발생하는 이벤트별 처리"""

    def on_created(self, event):
        if event.is_directory: return
        p = Path(event.src_path)
        if p.suffix.lower() in ALLOW_EXTS and not is_lock_like(p):
            schedule_upload(p)

    def on_moved(self, event):
        if event.is_directory: return
        p = Path(event.dest_path)
        if p.suffix.lower() in ALLOW_EXTS and not is_lock_like(p):
            schedule_upload(p)

    def on_modified(self, event):
        if ONLY_MOVE_CREATE:
            return  # 수정 이벤트는 무시
        if event.is_directory: return
        p = Path(event.src_path)
        if p.suffix.lower() in ALLOW_EXTS and not is_lock_like(p):
            schedule_upload(p)

    def on_deleted(self, event):
        if event.is_directory: return
        p = Path(event.src_path)
        if p.suffix.lower() in ALLOW_EXTS and not is_lock_like(p):
            remove_from_vector_store(p)


# ---------- 초기 스캔 ----------
def initial_scan():
    """프로그램 시작 시 이미 있는 파일도 업로드"""
    for p in DOCS_DIR.rglob("*"):
        if p.is_file() and p.suffix.lower() in ALLOW_EXTS and not is_lock_like(p):
            schedule_upload(p)


# ---------- 메인 ----------
if __name__ == "__main__":
    print(f"[WATCH] dir={DOCS_DIR.resolve()}  vs_id={VS_ID}")
    initial_scan()
    obs = Observer()
    handler = DocEventHandler()
    obs.schedule(handler, str(DOCS_DIR), recursive=True)  # 하위 폴더까지 감시
    obs.start()
    try:
        while True:
            time.sleep(1)  # 메인 루프 유지
    except KeyboardInterrupt:
        obs.stop()
    obs.join()
