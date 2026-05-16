import csv
import hashlib
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


LUA_REQUIRE_RE = re.compile(r"""require\s*\(?\s*["']([^"']+)["']\s*\)?""")
LUA_EVENTS_RE = re.compile(r"""Events\.([A-Za-z0-9_]+)\.Add\s*\(""")
LUA_CLASS_RE = re.compile(r"""([A-Za-z0-9_\.]+)\s*=\s*([A-Za-z0-9_\.]+)\s*or\s*\{\}""")
SCRIPT_ITEM_RE = re.compile(r"""^\s*item\s+([A-Za-z0-9_\-\.]+)\s*""", re.MULTILINE)
SCRIPT_RECIPE_RE = re.compile(r"""^\s*recipe\s+([^\{\n]+)\s*""", re.MULTILINE)
SCRIPT_VEHICLE_RE = re.compile(r"""^\s*vehicle\s+([A-Za-z0-9_\-\.]+)\s*""", re.MULTILINE)
SCRIPT_MODULE_RE = re.compile(r"""^\s*module\s+([A-Za-z0-9_\-\.]+)\s*""", re.MULTILINE)

CACHE_FILE = Path.home() / ".pz_mod_conflict_checker_cache.json"


@dataclass
class ModInfo:
    root: Path
    mod_id: str
    name: str
    dependencies: List[str] = field(default_factory=list)
    lua_files: List[Path] = field(default_factory=list)
    script_files: List[Path] = field(default_factory=list)
    map_dirs: List[Path] = field(default_factory=list)
    defines_items: Set[str] = field(default_factory=set)
    defines_recipes: Set[str] = field(default_factory=set)
    defines_vehicles: Set[str] = field(default_factory=set)
    lua_requires: Set[str] = field(default_factory=set)
    lua_events: Set[str] = field(default_factory=set)
    lua_globals: Set[str] = field(default_factory=set)


@dataclass
class Issue:
    severity: str
    score: int
    issue_type: str
    mods: str
    detail: str
    files: str


def read_text(path: Path) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp949", "latin-1"):
        try:
            return path.read_text(encoding=enc, errors="replace")
        except Exception:
            pass
    return ""


def parse_mod_info(mod_info_path: Path) -> Tuple[str, str, List[str]]:
    text = read_text(mod_info_path)
    mod_id = mod_info_path.parent.name
    name = mod_id
    dependencies: List[str] = []

    for line in text.splitlines():
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip().lower()
        value = value.strip()

        if key == "id" and value:
            mod_id = value
        elif key == "name" and value:
            name = value
        elif key in ("require", "requires", "dependencies") and value:
            dependencies.extend(v.strip() for v in re.split(r"[;,]", value) if v.strip())

    return mod_id, name, dependencies


def fast_list_files(folder: Path, suffixes: Tuple[str, ...]) -> List[Path]:
    if not folder.exists():
        return []

    results: List[Path] = []
    stack = [folder]
    suffixes = tuple(s.lower() for s in suffixes)

    while stack:
        cur = stack.pop()
        try:
            for p in cur.iterdir():
                if p.is_dir():
                    stack.append(p)
                elif p.suffix.lower() in suffixes:
                    results.append(p)
        except Exception:
            continue

    return results


def discover_mod_roots(input_path: Path) -> List[Path]:
    candidates: List[Path] = []

    if (input_path / "mod.info").exists():
        candidates.append(input_path)

    if input_path.exists() and input_path.is_dir():
        for p in input_path.iterdir():
            if not p.is_dir():
                continue

            if (p / "mod.info").exists():
                candidates.append(p)

            mods_dir = p / "mods"
            if mods_dir.exists():
                try:
                    for sub in mods_dir.iterdir():
                        if sub.is_dir() and (sub / "mod.info").exists():
                            candidates.append(sub)
                except Exception:
                    pass

    mods_dir = input_path / "mods"
    if mods_dir.exists():
        try:
            for sub in mods_dir.iterdir():
                if sub.is_dir() and (sub / "mod.info").exists():
                    candidates.append(sub)
        except Exception:
            pass

    unique = []
    seen = set()
    for c in candidates:
        try:
            resolved = str(c.resolve())
        except Exception:
            resolved = str(c)
        if resolved not in seen:
            seen.add(resolved)
            unique.append(c)

    return unique


def mod_fingerprint(root: Path) -> str:
    parts = [str(root.resolve())]
    mod_info = root / "mod.info"

    if mod_info.exists():
        st = mod_info.stat()
        parts.append(f"modinfo:{st.st_mtime_ns}:{st.st_size}")

    for folder_name in ("media/lua", "media/scripts", "media/maps"):
        folder = root / folder_name
        if not folder.exists():
            continue

        count = 0
        max_mtime = 0
        total_size = 0
        stack = [folder]

        while stack:
            cur = stack.pop()
            try:
                for p in cur.iterdir():
                    if p.is_dir():
                        stack.append(p)
                    else:
                        count += 1
                        try:
                            st = p.stat()
                            max_mtime = max(max_mtime, st.st_mtime_ns)
                            total_size += st.st_size
                        except Exception:
                            pass
            except Exception:
                pass

        parts.append(f"{folder_name}:{count}:{max_mtime}:{total_size}")

    return hashlib.sha1("|".join(parts).encode("utf-8", errors="ignore")).hexdigest()


def load_cache() -> Dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cache(cache: Dict) -> None:
    try:
        CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def serialize_mod(m: ModInfo) -> Dict:
    return {
        "root": str(m.root),
        "mod_id": m.mod_id,
        "name": m.name,
        "dependencies": m.dependencies,
        "lua_files": [str(p) for p in m.lua_files],
        "script_files": [str(p) for p in m.script_files],
        "map_dirs": [str(p) for p in m.map_dirs],
        "defines_items": sorted(m.defines_items),
        "defines_recipes": sorted(m.defines_recipes),
        "defines_vehicles": sorted(m.defines_vehicles),
        "lua_requires": sorted(m.lua_requires),
        "lua_events": sorted(m.lua_events),
        "lua_globals": sorted(m.lua_globals),
    }


def deserialize_mod(d: Dict) -> ModInfo:
    return ModInfo(
        root=Path(d["root"]),
        mod_id=d["mod_id"],
        name=d["name"],
        dependencies=d.get("dependencies", []),
        lua_files=[Path(p) for p in d.get("lua_files", [])],
        script_files=[Path(p) for p in d.get("script_files", [])],
        map_dirs=[Path(p) for p in d.get("map_dirs", [])],
        defines_items=set(d.get("defines_items", [])),
        defines_recipes=set(d.get("defines_recipes", [])),
        defines_vehicles=set(d.get("defines_vehicles", [])),
        lua_requires=set(d.get("lua_requires", [])),
        lua_events=set(d.get("lua_events", [])),
        lua_globals=set(d.get("lua_globals", [])),
    )


def parse_script_file(path: Path) -> Tuple[Set[str], Set[str], Set[str]]:
    text = read_text(path)
    module_match = SCRIPT_MODULE_RE.search(text)
    module_name = module_match.group(1) if module_match else ""

    items = {f"{module_name}.{x}" if module_name else x for x in SCRIPT_ITEM_RE.findall(text)}
    recipes = {x.strip() for x in SCRIPT_RECIPE_RE.findall(text)}
    vehicles = {f"{module_name}.{x}" if module_name else x for x in SCRIPT_VEHICLE_RE.findall(text)}
    return items, recipes, vehicles


def parse_lua_file(path: Path) -> Tuple[Set[str], Set[str], Set[str]]:
    text = read_text(path)
    return (
        set(LUA_REQUIRE_RE.findall(text)),
        set(LUA_EVENTS_RE.findall(text)),
        {m.group(1) for m in LUA_CLASS_RE.finditer(text)},
    )


def parse_mod_root(root: Path, cache: Dict, use_cache: bool = True) -> ModInfo:
    fingerprint = mod_fingerprint(root)
    cache_key = str(root.resolve())

    if use_cache and cache_key in cache and cache[cache_key].get("fingerprint") == fingerprint:
        return deserialize_mod(cache[cache_key]["data"])

    mod_id, name, deps = parse_mod_info(root / "mod.info")
    info = ModInfo(root=root, mod_id=mod_id, name=name, dependencies=deps)

    media = root / "media"
    lua_dir = media / "lua"
    scripts_dir = media / "scripts"
    maps_dir = media / "maps"

    info.lua_files = fast_list_files(lua_dir, (".lua",))
    info.script_files = fast_list_files(scripts_dir, (".txt", ".scripts"))

    if maps_dir.exists():
        try:
            info.map_dirs = [p for p in maps_dir.iterdir() if p.is_dir()]
        except Exception:
            info.map_dirs = []

    parse_targets = [("script", f) for f in info.script_files]
    parse_targets.extend(("lua", f) for f in info.lua_files)

    if parse_targets:
        workers = min(16, max(4, len(parse_targets)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {
                ex.submit(parse_script_file if kind == "script" else parse_lua_file, f): kind
                for kind, f in parse_targets
            }

            for fut in as_completed(futures):
                kind = futures[fut]
                try:
                    a, b, c = fut.result()
                    if kind == "script":
                        info.defines_items.update(a)
                        info.defines_recipes.update(b)
                        info.defines_vehicles.update(c)
                    else:
                        info.lua_requires.update(a)
                        info.lua_events.update(b)
                        info.lua_globals.update(c)
                except Exception:
                    continue

    cache[cache_key] = {
        "fingerprint": fingerprint,
        "data": serialize_mod(info),
    }
    return info


def discover_mods(input_path: Path, use_cache: bool = True) -> List[ModInfo]:
    roots = discover_mod_roots(input_path)
    cache = load_cache() if use_cache else {}
    mods: List[ModInfo] = []

    workers = min(16, max(4, len(roots) if roots else 4))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(parse_mod_root, root, cache, use_cache) for root in roots]
        for fut in as_completed(futs):
            try:
                mods.append(fut.result())
            except Exception:
                continue

    if use_cache:
        save_cache(cache)

    mods.sort(key=lambda m: m.name.lower())
    return mods


def severity_from_score(score: int) -> str:
    if score >= 90:
        return "HIGH"
    if score >= 60:
        return "MEDIUM"
    return "LOW"


def analyze(mods: List[ModInfo]) -> List[Issue]:
    issues: List[Issue] = []
    mod_by_id = {m.mod_id: m for m in mods}
    mod_by_name = {m.name: m for m in mods}

    for m in mods:
        for dep in m.dependencies:
            if dep not in mod_by_id and dep not in mod_by_name:
                issues.append(Issue(
                    severity="HIGH",
                    score=95,
                    issue_type="필수 모드 누락 의심",
                    mods=m.name,
                    detail=f"{m.name}의 mod.info에 require={dep}가 있지만 현재 입력 경로에서 발견되지 않았습니다.",
                    files=str(m.root / "mod.info"),
                ))

    rel_lua: Dict[str, List[Tuple[ModInfo, Path]]] = {}
    for m in mods:
        base = m.root / "media" / "lua"
        for f in m.lua_files:
            try:
                rel = str(f.relative_to(base)).replace("\\", "/")
            except ValueError:
                rel = str(f.relative_to(m.root)).replace("\\", "/")
            rel_lua.setdefault(rel.lower(), []).append((m, f))

    for rel, entries in rel_lua.items():
        uniq_mods = {e[0].name for e in entries}
        if len(uniq_mods) >= 2:
            issues.append(Issue(
                severity="HIGH",
                score=90,
                issue_type="동일 Lua 파일 경로 중복",
                mods=", ".join(sorted(uniq_mods)),
                detail=f"여러 모드가 같은 Lua 상대 경로를 사용합니다: {rel}. 로드 순서에 따라 한쪽 파일이 다른 쪽 파일을 덮을 수 있습니다.",
                files=" | ".join(str(e[1]) for e in entries[:10]),
            ))

    def duplicate_defs(label: str, attr: str, score: int) -> None:
        defs: Dict[str, List[ModInfo]] = {}
        for m in mods:
            for v in getattr(m, attr):
                defs.setdefault(v.lower(), []).append(m)
        for key, ms in defs.items():
            uniq = {m.name for m in ms}
            if len(uniq) >= 2:
                issues.append(Issue(
                    severity=severity_from_score(score),
                    score=score,
                    issue_type=f"{label} 중복 정의",
                    mods=", ".join(sorted(uniq)),
                    detail=f"여러 모드가 같은 {label}을 정의합니다: {key}. 밸런스, 속성, 제작법 덮어쓰기 문제가 생길 수 있습니다.",
                    files=" | ".join(str(m.root) for m in ms[:10]),
                ))

    duplicate_defs("아이템", "defines_items", 80)
    duplicate_defs("차량", "defines_vehicles", 75)
    duplicate_defs("레시피", "defines_recipes", 70)

    event_map: Dict[str, List[ModInfo]] = {}
    for m in mods:
        for ev in m.lua_events:
            event_map.setdefault(ev, []).append(m)

    for ev, ms in event_map.items():
        uniq = {m.name for m in ms}
        if len(uniq) >= 4:
            issues.append(Issue(
                severity="LOW",
                score=35,
                issue_type="동일 Lua 이벤트 다중 후킹",
                mods=", ".join(sorted(uniq)),
                detail=f"여러 모드가 Events.{ev}.Add를 사용합니다. 자체로 충돌은 아니지만 UI, 인벤토리, 월드 로딩 관련 이벤트라면 확인이 필요합니다.",
                files="",
            ))

    global_map: Dict[str, List[ModInfo]] = {}
    for m in mods:
        for g in m.lua_globals:
            if len(g) >= 4:
                global_map.setdefault(g.lower(), []).append(m)

    for g, ms in global_map.items():
        uniq = {m.name for m in ms}
        if len(uniq) >= 2:
            issues.append(Issue(
                severity="MEDIUM",
                score=60,
                issue_type="Lua 전역/네임스페이스 중복",
                mods=", ".join(sorted(uniq)),
                detail=f"여러 모드가 비슷한 Lua 전역 테이블을 정의합니다: {g}. 같은 라이브러리 내장 또는 덮어쓰기 가능성이 있습니다.",
                files=" | ".join(str(m.root) for m in ms[:10]),
            ))

    map_map: Dict[str, List[ModInfo]] = {}
    for m in mods:
        for d in m.map_dirs:
            map_map.setdefault(d.name.lower(), []).append(m)

    for map_name, ms in map_map.items():
        uniq = {m.name for m in ms}
        if len(uniq) >= 2:
            issues.append(Issue(
                severity="MEDIUM",
                score=65,
                issue_type="맵 폴더명 중복",
                mods=", ".join(sorted(uniq)),
                detail=f"같은 맵 폴더명이 여러 모드에 있습니다: {map_name}. 맵 로드 순서 충돌 가능성이 있습니다.",
                files=" | ".join(str(m.root / "media" / "maps" / map_name) for m in ms[:10]),
            ))

    issues.sort(key=lambda x: x.score, reverse=True)
    return issues


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Project Zomboid Mod Conflict Checker FAST")
        self.geometry("1240x740")
        self.minsize(980, 560)
        self.issues: List[Issue] = []
        self.filtered_issues: List[Issue] = []
        self.mods: List[ModInfo] = []
        self._sort_reverse: Dict[str, bool] = {}
        self.detail_window: Optional[tk.Toplevel] = None
        self.detail_text: Optional[tk.Text] = None
        self.detail_copy_value = ""

        self.path_var = tk.StringVar()
        self.search_var = tk.StringVar()
        self.cache_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="모드 폴더를 선택하세요. 캐시를 사용하면 두 번째 검사부터 더 빠릅니다.")

        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="모드 설치 경로").pack(side="left")
        ttk.Entry(top, textvariable=self.path_var, width=95).pack(side="left", padx=8, fill="x", expand=True)
        ttk.Button(top, text="찾기", command=self.browse).pack(side="left", padx=4)
        ttk.Checkbutton(top, text="캐시 사용", variable=self.cache_var).pack(side="left", padx=4)
        self.scan_button = ttk.Button(top, text="검사 실행", command=self.run_scan)
        self.scan_button.pack(side="left", padx=4)
        ttk.Button(top, text="CSV 저장", command=self.export_csv).pack(side="left", padx=4)
        ttk.Button(top, text="캐시 삭제", command=self.clear_cache).pack(side="left", padx=4)

        search_frame = ttk.Frame(self, padding=(10, 0, 10, 6))
        search_frame.pack(fill="x")
        ttk.Label(search_frame, text="모드 이름 검색").pack(side="left")
        search_entry = ttk.Entry(search_frame, textvariable=self.search_var, width=40)
        search_entry.pack(side="left", padx=8)
        ttk.Button(search_frame, text="검색 초기화", command=self.clear_search).pack(side="left")
        self.search_var.trace_add("write", lambda *_: self.apply_filter())

        columns = ("severity", "score", "type", "mods")
        table_frame = ttk.Frame(self)
        table_frame.pack(fill="both", expand=True, padx=10, pady=8)
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings")
        y_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        x_scroll = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)

        for col, title in [
            ("severity", "위험도"),
            ("score", "점수"),
            ("type", "유형"),
            ("mods", "관련 모드"),
        ]:
            self.tree.heading(col, text=title, command=lambda c=col: self.sort_by_column(c))

        self.tree.column("severity", width=80, anchor="center", stretch=False)
        self.tree.column("score", width=60, anchor="center", stretch=False)
        self.tree.column("type", width=260, stretch=False)
        self.tree.column("mods", width=760, stretch=True)

        self.tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")

        self.tree.tag_configure("HIGH", background="#ffe8e8")
        self.tree.tag_configure("MEDIUM", background="#fff5d6")
        self.tree.tag_configure("LOW", background="#eef6ff")
        self.tree.bind("<ButtonRelease-1>", self.on_tree_click)
        self.tree.bind("<Double-1>", self.show_selected_detail)
        self.tree.bind("<Control-c>", self.copy_selected_rows)
        self.tree.bind("<Control-C>", self.copy_selected_rows)

        bottom = ttk.Frame(self, padding=10)
        bottom.pack(fill="x")
        ttk.Label(bottom, textvariable=self.status_var).pack(side="left")
        ttk.Label(bottom, text="관련 모드 클릭: 상세 보기 / Ctrl+C: 선택 행 복사").pack(side="right")

    def browse(self) -> None:
        path = filedialog.askdirectory()
        if path:
            self.path_var.set(path)

    def clear_cache(self) -> None:
        try:
            if CACHE_FILE.exists():
                CACHE_FILE.unlink()
            messagebox.showinfo("완료", "캐시를 삭제했습니다.")
        except Exception as e:
            messagebox.showerror("오류", str(e))

    def run_scan(self) -> None:
        path = Path(self.path_var.get().strip())
        if not path.exists():
            messagebox.showerror("오류", "입력한 경로가 존재하지 않습니다.")
            return

        self.status_var.set("검사 중... 파일 수가 많으면 첫 실행에 시간이 걸릴 수 있습니다.")
        self.scan_button.configure(state="disabled")
        self.tree.delete(*self.tree.get_children())

        def worker() -> None:
            try:
                mods = discover_mods(path, use_cache=self.cache_var.get())
                issues = analyze(mods)
                self.after(0, lambda: self.finish_scan(mods, issues))
            except Exception as e:
                self.after(0, lambda error=e: self.scan_failed(error))

        threading.Thread(target=worker, daemon=True).start()

    def scan_failed(self, error: Exception) -> None:
        self.scan_button.configure(state="normal")
        messagebox.showerror("검사 실패", str(error))
        self.status_var.set("검사 실패")

    def finish_scan(self, mods: List[ModInfo], issues: List[Issue]) -> None:
        self.mods = mods
        self.issues = issues
        self.filtered_issues = issues
        self.scan_button.configure(state="normal")
        self.apply_filter()

    def render_results(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for index, issue in enumerate(self.filtered_issues):
            self.tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    issue.severity,
                    issue.score,
                    issue.issue_type,
                    issue.mods,
                ),
                tags=(issue.severity,),
            )
        query = self.search_var.get().strip()
        if query:
            self.status_var.set(
                f"검사 완료: 모드 {len(self.mods)}개, 충돌 의심 {len(self.issues)}건, 검색 결과 {len(self.filtered_issues)}건"
            )
        else:
            self.status_var.set(f"검사 완료: 모드 {len(self.mods)}개, 충돌 의심 {len(self.issues)}건")

    def apply_filter(self) -> None:
        query = self.search_var.get().strip().lower()
        if not query:
            self.filtered_issues = list(self.issues)
        else:
            self.filtered_issues = [issue for issue in self.issues if query in issue.mods.lower()]
        self.render_results()

    def clear_search(self) -> None:
        self.search_var.set("")

    def sort_by_column(self, col: str) -> None:
        rows = [(self.tree.set(item, col), item) for item in self.tree.get_children("")]
        reverse = self._sort_reverse.get(col, False)

        if col == "score":
            rows.sort(key=lambda row: int(row[0]) if str(row[0]).isdigit() else 0, reverse=reverse)
        else:
            rows.sort(key=lambda row: str(row[0]).lower(), reverse=reverse)

        for index, (_, item) in enumerate(rows):
            self.tree.move(item, "", index)

        self._sort_reverse[col] = not reverse

    def selected_rows_text(self) -> str:
        selected = self.tree.selection()
        if not selected:
            return ""

        rows = []
        for item in selected:
            rows.append("\t".join(str(value) for value in self.tree.item(item, "values")))
        return "\n".join(rows)

    def copy_selected_rows(self, event=None) -> str:
        text = self.selected_rows_text()
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.status_var.set("선택한 결과를 클립보드에 복사했습니다.")
        return "break"

    def show_selected_detail(self, event=None) -> None:
        selected = self.tree.selection()
        if not selected:
            return

        self.open_issue_detail(selected[0])

    def on_tree_click(self, event=None) -> None:
        if event is None:
            return

        if self.tree.identify_region(event.x, event.y) != "cell":
            return

        if self.tree.identify_column(event.x) != "#4":
            return

        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            self.open_issue_detail(item)

    def issue_from_item(self, item: str) -> Optional[Issue]:
        try:
            return self.filtered_issues[int(item)]
        except (ValueError, IndexError):
            return None

    def open_issue_detail(self, item: str) -> None:
        issue = self.issue_from_item(item)
        if issue is None:
            return

        detail = (
            f"위험도\n{issue.severity}\n\n"
            f"점수\n{issue.score}\n\n"
            f"유형\n{issue.issue_type}\n\n"
            f"관련 모드\n{issue.mods}\n\n"
            f"오류 내용\n{issue.detail}\n\n"
            f"파일 위치\n{issue.files or '표시할 파일 위치가 없습니다.'}"
        )
        self.ensure_detail_window()
        self.update_detail_window(detail)

    def ensure_detail_window(self) -> None:
        if self.detail_window is not None and self.detail_window.winfo_exists():
            self.detail_window.deiconify()
            self.detail_window.lift()
            self.detail_window.focus_force()
            return

        win = tk.Toplevel(self)
        win.title("충돌 의심 상세")
        win.geometry("860x520")
        win.minsize(680, 420)
        win.protocol("WM_DELETE_WINDOW", self.close_detail_window)

        outer = ttk.Frame(win, padding=12)
        outer.pack(fill="both", expand=True)
        outer.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)

        text = tk.Text(outer, wrap="word", height=20)
        y_scroll = ttk.Scrollbar(outer, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=y_scroll.set)
        text.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")

        button_frame = ttk.Frame(outer)
        button_frame.grid(row=1, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(button_frame, text="내용 복사", command=self.copy_current_detail).pack(side="left", padx=4)
        ttk.Button(button_frame, text="닫기", command=self.close_detail_window).pack(side="left", padx=4)

        self.detail_window = win
        self.detail_text = text

    def update_detail_window(self, detail: str) -> None:
        if self.detail_text is None:
            return

        self.detail_copy_value = detail
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("1.0", detail)
        self.detail_text.configure(state="disabled")

    def close_detail_window(self) -> None:
        if self.detail_window is not None and self.detail_window.winfo_exists():
            self.detail_window.destroy()
        self.detail_window = None
        self.detail_text = None
        self.detail_copy_value = ""

    def copy_current_detail(self) -> None:
        if self.detail_copy_value:
            self.copy_text(self.detail_copy_value)

    def copy_text(self, text: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status_var.set("상세 내용을 클립보드에 복사했습니다.")

    def export_csv(self) -> None:
        if not self.issues:
            messagebox.showinfo("안내", "저장할 검사 결과가 없습니다.")
            return

        file_path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")],
            initialfile="pz_mod_conflict_report.csv",
        )
        if not file_path:
            return

        with open(file_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["severity", "score", "issue_type", "mods", "detail", "files"])
            for i in self.issues:
                writer.writerow([i.severity, i.score, i.issue_type, i.mods, i.detail, i.files])

        messagebox.showinfo("완료", f"CSV 저장 완료\n{file_path}")


if __name__ == "__main__":
    App().mainloop()
