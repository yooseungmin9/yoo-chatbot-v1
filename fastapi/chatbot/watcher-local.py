# watcher_local.py — 문서 폴더 감시 → FAISS 벡터스토어 재생성
# 동작: ./docs 변화 감지 → 전체 문서 재로드 → 임베딩 생성 → FAISS 인덱스 저장

import os, time, json, threading, hashlib
from pathlib import Path
from typing import Dict
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from dotenv import load_dotenv

# LangChain imports
from langchain_community.vectorstores import FAISS
from langchain_community.document_loaders import DirectoryLoader, UnstructuredWordDocumentLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_huggingface import HuggingFaceEmbeddings

# ===== 환경변수 로드 =====
load_dotenv(override=True)

# ===== 기본 경로/상태 파일 =====
DOCS_DIR = Path("./docs")
VECTOR_STORE_DIR = Path("./vector_store")
STATE_FILE = Path(".vs_state.json")

# ===== 이벤트 처리 정책 =====
DEBOUNCE_SECS = 3.0  # 여러 파일 변경을 하나의 재생성으로 합치기
DWELL_SECS = 2.0     # 파일 안정화 대기 시간

# ===== 파일 필터 =====
ALLOW_EXTS = {".pdf", ".docx", ".pptx", ".txt", ".md"}
LOCK_PREFIXES = ("~$",)
LOCK_SUFFIXES = (".tmp", ".part", ".lock")

# ===== 임베딩 모델 초기화 =====
print("[INIT] Loading embedding model...")
embeddings = HuggingFaceEmbeddings(
    model_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
)
print("[INIT] Embedding model loaded")

# ===== 텍스트 분할기 =====
text_splitter = RecursiveCharacterTextSplitter(
    chunk_size=500,
    chunk_overlap=50
)

# ===== 상태 관리 =====
def load_state() -> Dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"files": {}, "last_rebuild": None}

def save_state(state: Dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

state = load_state()
DOCS_DIR.mkdir(parents=True, exist_ok=True)
VECTOR_STORE_DIR.mkdir(exist_ok=True)

# ===== 유틸 =====
def is_lock_like(p: Path) -> bool:
    name = p.name
    return name.startswith(LOCK_PREFIXES) or p.suffix.lower() in LOCK_SUFFIXES

def file_key(p: Path) -> str:
    return str(p.resolve()).lower()

def stable_hash(p: Path) -> str:
    """파일 해시 계산"""
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def is_stable(p: Path, dwell=DWELL_SECS) -> bool:
    """파일이 안정적인지 확인 (저장 중이 아닌지)"""
    try:
        s1 = (p.stat().st_size, p.stat().st_mtime)
        time.sleep(dwell)
        s2 = (p.stat().st_size, p.stat().st_mtime)
        return s1 == s2
    except FileNotFoundError:
        return False

# ===== 벡터 스토어 재생성 =====
def rebuild_vector_store():
    """전체 문서를 다시 로드하여 FAISS 벡터 스토어 재생성"""
    print("[REBUILD] Starting vector store rebuild...")
    
    try:
        # 1. 유효한 파일만 필터링하여 로드
        valid_files = []
        for p in DOCS_DIR.rglob("*.docx"):
            # 잠금 파일과 임시 파일 제외
            if not is_lock_like(p) and p.is_file():
                valid_files.append(str(p))
        
        if not valid_files:
            print("[WARN] No valid documents found. Skipping rebuild.")
            return
        
        print(f"[REBUILD] Found {len(valid_files)} valid documents")
        
        # 2. 각 파일 개별 로드 (에러 발생 시 스킵)
        documents = []
        for file_path in valid_files:
            try:
                loader = UnstructuredWordDocumentLoader(file_path)
                docs = loader.load()
                documents.extend(docs)
                print(f"[REBUILD] ✓ Loaded: {Path(file_path).name}")
            except Exception as e:
                print(f"[WARN] Failed to load {Path(file_path).name}: {e}")
                continue
        
        if not documents:
            print("[WARN] No documents loaded successfully. Skipping rebuild.")
            return
        
        print(f"[REBUILD] Successfully loaded {len(documents)} documents")
        
        # 3. 텍스트 분할
        chunks = text_splitter.split_documents(documents)
        print(f"[REBUILD] Split into {len(chunks)} chunks")
        
        # 4. FAISS 벡터 스토어 생성
        vectorstore = FAISS.from_documents(chunks, embeddings)
        
        # 5. 로컬에 저장
        vectorstore.save_local(str(VECTOR_STORE_DIR))
        print(f"[REBUILD] Saved to {VECTOR_STORE_DIR}")
        
        # 6. 상태 업데이트
        new_state = {"files": {}, "last_rebuild": time.time()}
        for p in DOCS_DIR.rglob("*"):
            if p.is_file() and p.suffix.lower() in ALLOW_EXTS and not is_lock_like(p):
                try:
                    new_state["files"][file_key(p)] = {
                        "hash": stable_hash(p),
                        "name": p.name
                    }
                except Exception:
                    pass
        
        save_state(new_state)
        print("[REBUILD] ✓ Vector store rebuild complete")
        
    except Exception as e:
        print(f"[ERROR] Rebuild failed: {e}")
        import traceback
        traceback.print_exc()

# ===== 변경 감지 로직 =====
def has_changes() -> bool:
    """현재 파일 상태와 저장된 상태 비교"""
    current_files = {}
    for p in DOCS_DIR.rglob("*"):
        if p.is_file() and p.suffix.lower() in ALLOW_EXTS and not is_lock_like(p):
            try:
                if is_stable(p, dwell=1.0):  # 빠른 안정성 체크
                    current_files[file_key(p)] = stable_hash(p)
            except Exception:
                pass
    
    # 파일 개수나 해시가 다르면 변경됨
    for key, current_hash in current_files.items():
        old = state["files"].get(key, {})
        if old.get("hash") != current_hash:
            return True
    
    # 삭제된 파일 체크
    if set(state["files"].keys()) != set(current_files.keys()):
        return True
    
    return False

# ===== 디바운스 타이머 =====
_rebuild_timer = None

def schedule_rebuild():
    """디바운스: 여러 파일 변경을 하나의 재생성으로 합치기"""
    global _rebuild_timer
    
    if _rebuild_timer:
        _rebuild_timer.cancel()
    
    def rebuild_if_changed():
        if has_changes():
            print("[DEBOUNCE] Changes detected, triggering rebuild...")
            rebuild_vector_store()
        else:
            print("[DEBOUNCE] No actual changes, skipping rebuild")
    
    _rebuild_timer = threading.Timer(DEBOUNCE_SECS, rebuild_if_changed)
    _rebuild_timer.start()
    print(f"[DEBOUNCE] Rebuild scheduled in {DEBOUNCE_SECS}s")

# ===== 파일 필터 강화 =====
LOCK_PREFIXES = ("~$", "~", ".")  # macOS .DS_Store도 제외
LOCK_SUFFIXES = (".tmp", ".part", ".lock", ".swp", ".bak")

# ===== 이벤트 핸들러 =====
class DocEventHandler(FileSystemEventHandler):
    """파일 시스템 이벤트 → 재생성 스케줄"""
    
    def _should_process(self, path: Path) -> bool:
        """처리해야 할 파일인지 확인"""
        if not path.is_file():
            return False
        if path.suffix.lower() not in ALLOW_EXTS:
            return False
        if is_lock_like(path):
            return False
        return True
    
    def on_created(self, event):
        if event.is_directory:
            return
        p = Path(event.src_path)
        if self._should_process(p):
            print(f"[EVENT] Created: {p.name}")
            schedule_rebuild()
    
    def on_modified(self, event):
        if event.is_directory:
            return
        p = Path(event.src_path)
        if self._should_process(p):
            print(f"[EVENT] Modified: {p.name}")
            schedule_rebuild()
    
    def on_moved(self, event):
        if event.is_directory:
            return
        p = Path(event.dest_path)
        if self._should_process(p):
            print(f"[EVENT] Moved: {p.name}")
            schedule_rebuild()
    
    def on_deleted(self, event):
        if event.is_directory:
            return
        p = Path(event.src_path)
        # 삭제 이벤트는 파일 존재 여부 체크 불가, 확장자만 확인
        if p.suffix.lower() in ALLOW_EXTS and not is_lock_like(p):
            print(f"[EVENT] Deleted: {p.name}")
            schedule_rebuild()

# ===== 초기 빌드 =====
def initial_build():
    """시작 시 벡터 스토어가 없거나 변경사항이 있으면 빌드"""
    index_file = VECTOR_STORE_DIR / "index.faiss"
    
    if not index_file.exists():
        print("[INIT] No existing vector store, building...")
        rebuild_vector_store()
    elif has_changes():
        print("[INIT] Changes detected, rebuilding...")
        rebuild_vector_store()
    else:
        print("[INIT] Vector store up to date")

# ===== 메인 =====
if __name__ == "__main__":
    print(f"[WATCH] Monitoring: {DOCS_DIR.resolve()}")
    print(f"[WATCH] Vector store: {VECTOR_STORE_DIR.resolve()}")
    
    # 초기 빌드
    initial_build()
    
    # Watchdog 시작
    obs = Observer()
    handler = DocEventHandler()
    obs.schedule(handler, str(DOCS_DIR), recursive=True)
    obs.start()
    
    print("[WATCH] Watching for changes... (Ctrl+C to stop)")
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[WATCH] Stopping...")
        obs.stop()
    
    obs.join()
    print("[WATCH] Stopped")
