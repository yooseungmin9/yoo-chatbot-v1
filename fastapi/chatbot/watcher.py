# watcher.py — 문서 폴더 감시 → OpenAI 벡터스토어 업로드
# 동작: ./docs 변화 감지 → 안전 복사(.staging) → OpenAI Files 업로드 → Vector Store에 연결/갱신

import os, time, json, threading, hashlib, shutil
from pathlib import Path
from typing import Dict
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from openai import OpenAI

# ===== OpenAI API 키 =====
# 환경변수 OPENAI_API_KEY 사용(미설정이면 빈 문자열)
API_KEY = os.getenv("OPENAI_API_KEY", "")

# ===== 기본 경로/상태 파일 =====
# 감시 대상/스테이징/상태파일/벡터스토어ID 파일 경로 정의
DOCS_DIR = Path("./docs")
STAGING_DIR = Path(".staging")
STATE_FILE = Path(".vs_state.json")
VS_ID_FILE = Path(".vector_store_id")

# ===== 이벤트 처리 정책 =====
# 수정 이벤트 처리 방식/디바운스/안정화 대기시간 설정
ONLY_MOVE_CREATE = True
DEBOUNCE_SECS = 1.5
DWELL_SECS = 2.0

# ===== 파일 필터 =====
# 허용 확장자/잠금·임시파일 패턴
ALLOW_EXTS = {".pdf", ".docx", ".pptx", ".xlsx", ".txt", ".md"}
LOCK_PREFIXES = ("~$",)
LOCK_SUFFIXES = (".tmp", ".part", ".lock")

# ===== OpenAI 클라이언트/벡터스토어 이름 =====
# 기본 클라이언트 생성 + 사용할 벡터스토어 논리명
client = OpenAI(api_key = API_KEY)
VECTOR_STORE_NAME = "econ-news-spec-store"

# ===== 상태 관리 =====
# 업로드된 파일의 hash/file_id/VS_ID를 JSON으로 유지
def load_state() -> Dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"vector_store_id": None, "files": {}}

def save_state(state: Dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

state = load_state()

def ensure_vector_store() -> str:
    # 기존 VS_ID 재사용, 없으면 생성 후 파일로 기록
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
DOCS_DIR.mkdir(parents=True, exist_ok=True)
STAGING_DIR.mkdir(exist_ok=True)

# ===== 유틸 =====
# 잠금/임시파일 판별, 파일 해시/안정성 검사, 스테이징 안전 복사
def is_lock_like(p: Path) -> bool:
    name = p.name
    return name.startswith(LOCK_PREFIXES) or p.suffix.lower() in LOCK_SUFFIXES

def file_key(p: Path) -> str:
    return str(p.resolve()).lower()

def stable_hash(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def is_stable(p: Path, dwell=DWELL_SECS) -> bool:
    try:
        s1 = (p.stat().st_size, p.stat().st_mtime)
        time.sleep(dwell)
        s2 = (p.stat().st_size, p.stat().st_mtime)
        return s1 == s2
    except FileNotFoundError:
        return False

def safe_copy_to_staging(src: Path) -> Path:
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

# ===== 업로드/연결 =====
# 파일 안정화 → 스테이징 복사 → OpenAI Files 업로드 → Vector Store 연결/교체 → 상태 갱신
def upload_and_link(path: Path):
    if not path.exists():
        return
    if path.suffix.lower() not in ALLOW_EXTS or is_lock_like(path):
        return

    # 저장 중이면 재스케줄
    if not is_stable(path, dwell=DWELL_SECS):
        if not is_stable(path, dwell=DWELL_SECS):
            schedule_upload(path)
            return

    key = file_key(path)
    new_hash = stable_hash(path)
    old = state["files"].get(key, {})

    # 내용 동일 시 스킵
    if old.get("hash") == new_hash:
        print(f"[SKIP] unchanged: {path.name}")
        return

    print(f"[UPLOAD] {path.name} (hash changed)")

    # 스테이징 복사
    try:
        staging = safe_copy_to_staging(path)
    except Exception as e:
        print(f"[WARN] copy failed (locked?): {e}. reschedule.")
        schedule_upload(path)
        return

    try:
        # OpenAI 파일 업로드 → VS에 연결
        with staging.open("rb") as f:
            fobj = client.files.create(file=f, purpose="assistants")
        client.vector_stores.files.create(vector_store_id=VS_ID, file_id=fobj.id)

        # 이전 파일 정리
        old_file_id = old.get("file_id")
        if old_file_id:
            try:
                client.vector_stores.files.delete(vector_store_id=VS_ID, file_id=old_file_id)
                print(f"[CLEANUP] removed old file_id={old_file_id}")
            except Exception as e:
                print(f"[WARN] delete old failed: {e}")

        # 상태 저장
        state["files"][key] = {"file_id": fobj.id, "hash": new_hash, "name": path.name}
        save_state(state)
        print(f"[OK] linked {path.name} → file_id={fobj.id}")

    finally:
        try:
            staging.unlink(missing_ok=True)  # 스테이징 파일 삭제
        except Exception:
            pass

def remove_from_vector_store(path: Path):
    # 파일 삭제 이벤트 → VS에서도 제거, 상태 갱신
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


# ===== 디바운스 =====
# 다중 이벤트를 짧은 지연 후 단일 업로드로 합치기
_timers: Dict[str, threading.Timer] = {}

def schedule_upload(path: Path):
    key = file_key(path)
    t = _timers.get(key)
    if t:
        t.cancel()
    timer = threading.Timer(DEBOUNCE_SECS, upload_and_link, args=[path])
    _timers[key] = timer
    timer.start()

# ===== 이벤트 핸들러 =====
# 생성/이동/수정/삭제 이벤트에 맞춰 업로드/삭제 처리
class DocEventHandler(FileSystemEventHandler):

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
        if event.is_directory: return
        p = Path(event.src_path)
        if p.suffix.lower() in ALLOW_EXTS and not is_lock_like(p):
            schedule_upload(p)

    def on_deleted(self, event):
        if event.is_directory: return
        p = Path(event.src_path)
        if p.suffix.lower() in ALLOW_EXTS and not is_lock_like(p):
            remove_from_vector_store(p)

# ===== 초기 스캔 =====
# 시작 시 기존 파일들도 업로드 큐에 등록
def initial_scan():
    for p in DOCS_DIR.rglob("*"):
        if p.is_file() and p.suffix.lower() in ALLOW_EXTS and not is_lock_like(p):
            schedule_upload(p)

# ===== 메인 =====
# 감시 시작/루프 유지/종료 처리
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