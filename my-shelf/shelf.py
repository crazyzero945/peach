#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""我的片库 (my-shelf) —— 本地优先的媒体库服务。

从 Peach (https://github.com/peach-mitao/peach) 的产品思路出发的独立实现：
扫描本地目录、生成海报墙、做搜索与多维筛选，并保留人工修改。

设计取舍（相对 Peach 的简化与改进）：
  * 只用 Python 标准库，Python 3.9+ 即可运行（Peach 需要 3.14 + uv + 几十个依赖）。
  * 单文件服务 + 单文件前端，双击 start.bat 就能用，不装前端工具链。
  * 借用了 Peach 扫描层的三条不变量：
      - 只登记「作品」，海报 / 字幕 / NFO / 截图等附属文件一律不入库；
      - 扫描只刷新文件事实（大小、修改时间、最后见到），不覆盖人工编辑过的字段；
      * 文件消失只标记 missing，不删除记录，也不丢观看记录与收藏。
  * 新增：HTTP Range 串流播放、重复文件检测、CSV 导出、目录配置持久化、中文界面。

服务只监听 127.0.0.1，所有文件路径都从数据库取，不接受客户端传来的任意路径。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import mimetypes
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
DB_PATH = DATA_DIR / "shelf.db"
CONFIG_PATH = BASE / "config.json"
WEB_DIR = BASE / "web"
THUMB_DIR = DATA_DIR / "thumbs"

VIDEO_EXTS = frozenset({
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
    ".ts", ".m2ts", ".mts", ".rmvb", ".rm", ".mpg", ".mpeg", ".3gp",
    ".vob", ".divx", ".f4v", ".asf",
})
IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"})
SUBTITLE_EXTS = frozenset({".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx"})

# 跟 Peach 的 scan.py 同一口径：这些名字的图片是附属物，不是作品。
ARTWORK_STEMS = frozenset({
    "poster", "fanart", "thumb", "folder", "cover", "banner", "clearart",
    "clearlogo", "landscape", "disc", "discart", "logo", "backdrop",
    "keyart", "back", "front", "spine",
})
ARTWORK_DIRS = frozenset({"extrafanart", "extrathumbs", "behind the scenes", "thumbs"})
SKIP_DIRS = frozenset({
    "$recycle.bin", "system volume information", ".git", ".svn", "node_modules",
    "@eadir", "__macosx", ".trash", ".workbuddy", "extrafanart", "extrathumbs",
})
DERIVED_THUMB = re.compile(
    r"(?:_\d+x\d+_thumbs?|\.(?:mp4|mkv|avi|mov)_thumbs?|\.(?:jpg|jpeg|png)\.thumb)$", re.I)

CODE_RE = re.compile(r"(?:^|[^A-Za-z0-9])([A-Za-z]{2,6}[-_]?\d{2,6})(?:[^A-Za-z0-9]|$)")
YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
RES_RE = re.compile(r"\b(2160p|1080p|720p|480p|360p|4k|8k|uhd)\b", re.I)
NOISE_RE = re.compile(
    r"\b(1080p|720p|2160p|480p|360p|4k|8k|uhd|hdr|bluray|bdrip|brrip|web-?dl|webrip|"
    r"dvdrip|hdtv|x264|x265|h264|h265|hevc|aac|ac3|dts|10bit|8bit|60fps|"
    r"uncensored|censored|chinese|english|japanese|sub|chs|cht|gb|big5|remux|repack)\b",
    re.I)
BRACKETS_RE = re.compile(r"[\[\(【][^\]\)】]*[\]\)】]")
# `01-xxx` / `2024.xxx` 这类排序用目录名不适合当标签。
FOLDER_NOISE_RE = re.compile(r"^\d{1,4}[\s._\-—]+")

CONTENT_TYPES = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm",
    ".mov": "video/quicktime", ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo", ".wmv": "video/x-ms-wmv",
    ".flv": "video/x-flv", ".ts": "video/mp2t", ".m2ts": "video/mp2t",
    ".mpg": "video/mpeg", ".mpeg": "video/mpeg", ".3gp": "video/3gpp",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".bmp": "image/bmp", ".gif": "image/gif",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT NOT NULL UNIQUE,
    root        TEXT NOT NULL,
    title       TEXT NOT NULL,
    filename    TEXT NOT NULL,
    folder      TEXT NOT NULL,
    ext         TEXT NOT NULL,
    size        INTEGER NOT NULL DEFAULT 0,
    mtime       REAL NOT NULL DEFAULT 0,
    year        INTEGER,
    rating      REAL,
    tags        TEXT NOT NULL DEFAULT '[]',
    plot        TEXT NOT NULL DEFAULT '',
    cover       TEXT NOT NULL DEFAULT '',
    studio      TEXT NOT NULL DEFAULT '',
    actors      TEXT NOT NULL DEFAULT '[]',
    resolution  TEXT NOT NULL DEFAULT '',
    code        TEXT NOT NULL DEFAULT '',
    favorite    INTEGER NOT NULL DEFAULT 0,
    watched     INTEGER NOT NULL DEFAULT 0,
    progress    REAL NOT NULL DEFAULT 0,
    note        TEXT NOT NULL DEFAULT '',
    added_at    TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    edited      INTEGER NOT NULL DEFAULT 0,
    missing     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_assets_title  ON assets(title);
CREATE INDEX IF NOT EXISTS idx_assets_size   ON assets(size);
CREATE INDEX IF NOT EXISTS idx_assets_mtime  ON assets(mtime);
CREATE INDEX IF NOT EXISTS idx_assets_folder ON assets(folder);
CREATE INDEX IF NOT EXISTS idx_assets_missing ON assets(missing);
"""

# 一次全量写入用到的字段；added_at / favorite / watched / progress / note / edited 不在此列，
# 因为它们是「人的记录」，重扫不该碰。
WRITE_FIELDS = (
    "path", "root", "title", "filename", "folder", "ext", "size", "mtime",
    "year", "rating", "tags", "plot", "cover", "studio", "actors",
    "resolution", "code", "last_seen", "missing",
)
EDITABLE_FIELDS = frozenset({
    "title", "year", "rating", "tags", "plot", "note", "favorite",
    "watched", "progress", "cover",
})
METADATA_FIELDS = frozenset({"title", "year", "rating", "tags", "plot", "cover"})

CONFIG = {"roots": [], "port": 8765}
CONFIG_LOCK = threading.Lock()
SCAN = {
    "running": False, "scanned": 0, "added": 0, "updated": 0,
    "missing": 0, "current": "", "started_at": None,
    "finished_at": None, "error": None,
}
SCAN_LOCK = threading.Lock()


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def human_size(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


# --------------------------------------------------------------------------- 存储

def connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB_PATH), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


@contextmanager
def db():
    con = connect()
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.executescript(SCHEMA)


def load_config() -> dict:
    if CONFIG_PATH.is_file():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {"roots": list(data.get("roots") or []),
                        "port": int(data.get("port") or 8765)}
        except (ValueError, OSError, TypeError):
            pass
    return {"roots": [], "port": 8765}


def save_config() -> None:
    with CONFIG_LOCK:
        snapshot = {"roots": list(CONFIG["roots"]), "port": int(CONFIG["port"])}
    CONFIG_PATH.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")


def current_roots() -> list[str]:
    with CONFIG_LOCK:
        return list(CONFIG["roots"])


# --------------------------------------------------------------------------- 解析

def is_derived_thumb(name: str) -> bool:
    """截图工具产出的拼版缩略图不配当封面。

    图片一律不登记为作品，只作为封面候选：因此 poster.jpg / folder.jpg 这类
    通用海报要留在候选里，不能像 Peach 那样在「登记」阶段一并排除。
    """
    return bool(DERIVED_THUMB.search(Path(name).stem))


def parse_filename(filename: str) -> dict:
    stem = Path(filename).stem
    code_match = CODE_RE.search(stem)
    code = code_match.group(1).upper().replace("_", "-") if code_match else ""
    year_match = YEAR_RE.search(stem)
    year = int(year_match.group(1)) if year_match else None
    res_match = RES_RE.search(stem)
    raw_res = res_match.group(1).casefold() if res_match else ""
    resolution = {"2160p": "4K", "4k": "4K", "8k": "8K", "uhd": "4K"}.get(raw_res, raw_res)

    title = BRACKETS_RE.sub(" ", stem)
    if code_match:
        title = title.replace(code_match.group(1), " ")
    title = NOISE_RE.sub(" ", title)
    title = re.sub(r"[._]+", " ", title)
    title = re.sub(r"\s*[-–—]\s*", " — ", title)
    title = re.sub(r"\s+", " ", title).strip(" -–—_")
    if not title:
        title = stem

    tags: list[str] = []
    if resolution and resolution not in tags:
        tags.append(resolution)
    return {"code": code, "year": year, "resolution": resolution,
            "title": title, "tags": tags}


def parse_nfo(directory: Path, stem: str) -> dict:
    """读 Kodi / Jellyfin 风格的同名 .nfo，读不到就安静地返回空。"""
    for name in (stem + ".nfo", "movie.nfo"):
        path = directory / name
        if not path.is_file():
            continue
        try:
            root = ET.parse(str(path)).getroot()
        except (ET.ParseError, OSError, UnicodeDecodeError):
            continue

        def text(tag: str) -> str:
            node = root.find(tag)
            return (node.text or "").strip() if node is not None else ""

        year_text = text("year")
        if not year_text:
            found = YEAR_RE.search(text("premiered") or text("releasedate") or text("aired"))
            year_text = found.group(1) if found else ""
        rating_text = text("rating")
        try:
            rating = float(rating_text) if rating_text else None
        except ValueError:
            rating = None
        tags: list[str] = []
        for node in list(root.findall("genre")) + list(root.findall("tag")):
            value = (node.text or "").strip()
            if value and value not in tags:
                tags.append(value)
        actors: list[str] = []
        for node in root.findall("actor"):
            value = (node.findtext("name") or "").strip()
            if value and value not in actors:
                actors.append(value)
        try:
            year = int(year_text) if year_text else None
        except ValueError:
            year = None
        return {
            "title": text("title") or text("originaltitle"),
            "year": year,
            "rating": rating,
            "plot": text("plot") or text("outline"),
            "tags": tags,
            "actors": actors,
            "studio": text("studio") or text("maker") or text("label"),
        }
    return {}


def find_cover(images: dict[str, str], stem: str) -> str:
    """封面优先级：同名图 > 同名 -poster > 目录通用海报 > 同目录唯一图片。"""
    candidates = (
        stem + ".jpg", stem + ".jpeg", stem + ".png", stem + ".webp",
        stem + "-poster.jpg", stem + "-poster.png", stem + "-poster.webp",
        stem + "-thumb.jpg", stem + ".thumb.jpg",
        "poster.jpg", "poster.png", "poster.webp", "folder.jpg", "folder.png",
        "cover.jpg", "cover.png", "cover.webp", "fanart.jpg", "fanart.png",
        "thumb.jpg", "movie.jpg",
    )
    for name in candidates:
        hit = images.get(name.casefold())
        if hit:
            return hit
    return ""


def build_record(full: str, root: str, stat: os.stat_result, images: dict[str, str]) -> dict:
    path = Path(full)
    stem = path.stem
    parsed = parse_filename(path.name)
    nfo = parse_nfo(path.parent, stem)
    folder_name = path.parent.name

    title = nfo.get("title") or parsed["title"]
    year = nfo.get("year") or parsed["year"]
    tags: list[str] = []
    for value in list(nfo.get("tags") or []) + parsed["tags"]:
        if value and value not in tags:
            tags.append(value)
    # 把所在文件夹当成一个轻量分类，但不把媒体根本身塞进去。
    if (folder_name and folder_name != title and len(folder_name) <= 24
            and folder_name.casefold() != Path(root).name.casefold()
            and not FOLDER_NOISE_RE.match(folder_name)):
        if folder_name not in tags:
            tags.append(folder_name)

    return {
        "path": full,
        "root": root,
        "title": title,
        "filename": path.name,
        "folder": str(path.parent),
        "ext": path.suffix.casefold(),
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "year": year,
        "rating": nfo.get("rating"),
        "tags": json.dumps(tags, ensure_ascii=False),
        "plot": nfo.get("plot") or "",
        "cover": find_cover(images, stem),
        "studio": nfo.get("studio") or "",
        "actors": json.dumps(nfo.get("actors") or [], ensure_ascii=False),
        "resolution": parsed["resolution"],
        "code": parsed["code"],
        "last_seen": now_str(),
        "missing": 0,
        "added_at": now_str(),
    }


UPSERT_SQL = (
    "INSERT INTO assets (" + ",".join(WRITE_FIELDS) + ",added_at) VALUES ("
    + ",".join(":" + f for f in WRITE_FIELDS) + ",:added_at) "
    "ON CONFLICT(path) DO UPDATE SET "
    + ",".join(f"{f}=excluded.{f}" for f in WRITE_FIELDS if f != "path")
)


def upsert(con: sqlite3.Connection, full: str, root: str,
           stat: os.stat_result, images: dict[str, str]) -> str:
    """返回 added / updated / kept / same，供进度统计用。"""
    old = con.execute(
        "SELECT id,size,mtime,edited FROM assets WHERE path=?", (full,)
    ).fetchone()
    stamp = now_str()

    if old and old["size"] == stat.st_size and abs(old["mtime"] - stat.st_mtime) < 1:
        con.execute("UPDATE assets SET last_seen=?, missing=0 WHERE id=?", (stamp, old["id"]))
        return "same"

    if old and old["edited"]:
        # 人工改过标题 / 标签 / 简介：只刷新文件事实，绝不让重扫覆盖人的记录。
        con.execute(
            "UPDATE assets SET size=?, mtime=?, filename=?, folder=?, "
            "last_seen=?, missing=0 WHERE id=?",
            (stat.st_size, stat.st_mtime, Path(full).name,
             str(Path(full).parent), stamp, old["id"]),
        )
        return "kept"

    record = build_record(full, root, stat, images)
    con.execute(UPSERT_SQL, record)
    return "added" if old is None else "updated"


# --------------------------------------------------------------------------- 扫描

def scan_worker(roots: list[str]) -> None:
    counts = {"added": 0, "updated": 0, "kept": 0, "same": 0}
    seen: set[str] = set()
    con = None
    try:
        con = connect()
        pending = 0
        for root in roots:
            root_path = Path(root)
            if not root_path.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(str(root_path)):
                dirnames[:] = sorted(
                    d for d in dirnames
                    if d.casefold() not in SKIP_DIRS and not d.startswith(".")
                )
                names = list(filenames)
                video_names = [n for n in names if Path(n).suffix.casefold() in VIDEO_EXTS]
                images = {
                    n.casefold(): str(Path(dirpath) / n)
                    for n in names
                    if Path(n).suffix.casefold() in IMAGE_EXTS
                    and not is_derived_thumb(n)
                }
                for name in video_names:
                    full = str(Path(dirpath) / name)
                    try:
                        stat = os.stat(full)
                    except OSError:
                        continue
                    if stat.st_size == 0:
                        continue
                    seen.add(full)
                    result = upsert(con, full, root, stat, images)
                    counts[result] += 1
                    pending += 1
                    with SCAN_LOCK:
                        SCAN["scanned"] += 1
                        SCAN["added"] = counts["added"]
                        SCAN["updated"] = counts["updated"] + counts["kept"]
                        SCAN["current"] = name
                    if pending >= 200:
                        con.commit()
                        pending = 0
            con.commit()

        # 文件消失或目录被移除：只标记，不删记录。
        rows = con.execute("SELECT id,path,root,missing FROM assets").fetchall()
        missing = 0
        for row in rows:
            gone = row["path"] not in seen or row["root"] not in roots
            if gone and not row["missing"]:
                con.execute("UPDATE assets SET missing=1 WHERE id=?", (row["id"],))
                missing += 1
            elif not gone and row["missing"]:
                con.execute("UPDATE assets SET missing=0 WHERE id=?", (row["id"],))
        con.commit()
        with SCAN_LOCK:
            SCAN["missing"] = missing
    except Exception as error:  # noqa: BLE001 - 扫描失败要把原因带给前端
        traceback.print_exc()
        with SCAN_LOCK:
            SCAN["error"] = str(error)
    finally:
        if con is not None:
            try:
                con.commit()
            except sqlite3.Error:
                pass
            con.close()
        with SCAN_LOCK:
            SCAN["running"] = False
            SCAN["finished_at"] = now_str()
            SCAN["current"] = ""


def start_scan(roots: list[str] | None = None) -> bool:
    roots = list(roots if roots is not None else current_roots())
    with SCAN_LOCK:
        if SCAN["running"]:
            return False
        SCAN.update({
            "running": True, "scanned": 0, "added": 0, "updated": 0,
            "missing": 0, "current": "", "started_at": now_str(),
            "finished_at": None, "error": None,
        })
    threading.Thread(target=scan_worker, args=(roots,), daemon=True).start()
    return True


# --------------------------------------------------------------------------- 查询

SORT_COLUMNS = {
    "added": "added_at", "title": "title COLLATE NOCASE", "year": "year",
    "size": "size", "mtime": "mtime", "rating": "rating", "name": "filename COLLATE NOCASE",
}


def row_to_dict(row: sqlite3.Row) -> dict:
    item = dict(row)
    for field in ("tags", "actors"):
        try:
            item[field] = json.loads(item.get(field) or "[]")
        except (ValueError, TypeError):
            item[field] = []
    item["favorite"] = bool(item["favorite"])
    item["watched"] = bool(item["watched"])
    item["missing"] = bool(item["missing"])
    item["edited"] = bool(item["edited"])
    item["size_human"] = human_size(item["size"])
    item["mtime_human"] = (
        datetime.fromtimestamp(item["mtime"]).strftime("%Y-%m-%d")
        if item["mtime"] else ""
    )
    return item


def query_items(params: dict, cap: int = 200) -> dict:
    where: list[str] = []
    args: list[object] = []

    show = params.get("show", ["available"])[0]
    if show == "available":
        where.append("missing=0")
    elif show == "missing":
        where.append("missing=1")

    q = params.get("q", [""])[0].strip()
    for word in q.split():
        like = f"%{word}%"
        where.append(
            "(title LIKE ? OR filename LIKE ? OR plot LIKE ? OR tags LIKE ? "
            "OR actors LIKE ? OR code LIKE ? OR folder LIKE ? OR studio LIKE ?)"
        )
        args.extend([like] * 8)

    for tag in [t for t in params.get("tags", [""])[0].split("|") if t]:
        where.append("tags LIKE ?")
        args.append(f'%"{tag}"%')
    for key, column in (("year", "year"), ("ext", "ext"),
                        ("folder", "folder"), ("resolution", "resolution")):
        value = params.get(key, [""])[0]
        if value:
            where.append(f"{column}=?")
            args.append(value)
    for key, column in (("favorite", "favorite"), ("watched", "watched"),
                        ("edited", "edited")):
        value = params.get(key, [""])[0]
        if value in ("0", "1"):
            where.append(f"{column}=?")
            args.append(int(value))

    clause = (" WHERE " + " AND ".join(where)) if where else ""
    sort = params.get("sort", ["added"])[0]
    column = SORT_COLUMNS.get(sort, "added_at")
    direction = "ASC" if params.get("order", ["desc"])[0] == "asc" else "DESC"
    page = max(1, int(params.get("page", ["1"])[0] or 1))
    page_size = min(cap, max(1, int(params.get("page_size", ["60"])[0] or 60)))
    offset = (page - 1) * page_size

    with db() as con:
        total = con.execute("SELECT COUNT(*) FROM assets" + clause, args).fetchone()[0]
        rows = con.execute(
            f"SELECT * FROM assets{clause} ORDER BY {column} {direction}, id {direction} "
            "LIMIT ? OFFSET ?", (*args, page_size, offset),
        ).fetchall()
    return {
        "items": [row_to_dict(row) for row in rows],
        "total": total, "page": page, "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
    }


def facets() -> dict:
    with db() as con:
        rows = con.execute(
            "SELECT tags,year,ext,folder,resolution FROM assets WHERE missing=0"
        ).fetchall()
        duplicates = {
            row[0] for row in con.execute(
                "SELECT size FROM assets WHERE missing=0 AND size>0 "
                "GROUP BY size HAVING COUNT(*)>1"
            ).fetchall()
        }
    tags: dict[str, int] = {}
    years: dict[str, int] = {}
    exts: dict[str, int] = {}
    folders: dict[str, int] = {}
    resolutions: dict[str, int] = {}
    for row in rows:
        try:
            for tag in json.loads(row["tags"] or "[]"):
                tags[tag] = tags.get(tag, 0) + 1
        except (ValueError, TypeError):
            pass
        if row["year"]:
            key = str(row["year"])
            years[key] = years.get(key, 0) + 1
        if row["ext"]:
            exts[row["ext"]] = exts.get(row["ext"], 0) + 1
        if row["folder"]:
            folders[row["folder"]] = folders.get(row["folder"], 0) + 1
        if row["resolution"]:
            resolutions[row["resolution"]] = resolutions.get(row["resolution"], 0) + 1

    def ranked(mapping: dict) -> list[dict]:
        return [{"value": k, "count": v} for k, v in
                sorted(mapping.items(), key=lambda kv: (-kv[1], kv[0]))]

    return {
        "tags": ranked(tags), "years": ranked(years), "exts": ranked(exts),
        "folders": ranked(folders), "resolutions": ranked(resolutions),
        "duplicate_sizes": sorted(duplicates, reverse=True)[:200],
    }


def stats() -> dict:
    with db() as con:
        row = con.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(size),0) bytes FROM assets WHERE missing=0"
        ).fetchone()
        watched = con.execute(
            "SELECT COUNT(*) FROM assets WHERE missing=0 AND watched=1").fetchone()[0]
        favorite = con.execute(
            "SELECT COUNT(*) FROM assets WHERE missing=0 AND favorite=1").fetchone()[0]
        missing = con.execute("SELECT COUNT(*) FROM assets WHERE missing=1").fetchone()[0]
        edited = con.execute("SELECT COUNT(*) FROM assets WHERE edited=1").fetchone()[0]
        by_ext = con.execute(
            "SELECT ext, COUNT(*) n FROM assets WHERE missing=0 GROUP BY ext ORDER BY n DESC"
        ).fetchall()
        by_year = con.execute(
            "SELECT year, COUNT(*) n FROM assets WHERE missing=0 AND year IS NOT NULL "
            "GROUP BY year ORDER BY year DESC LIMIT 12"
        ).fetchall()
    return {
        "count": row["n"], "bytes": row["bytes"],
        "size_human": human_size(row["bytes"]),
        "watched": watched, "favorite": favorite, "missing": missing,
        "edited": edited, "roots": len(current_roots()),
        "by_ext": [{"value": r["ext"], "count": r["n"]} for r in by_ext],
        "by_year": [{"value": r["year"], "count": r["n"]} for r in by_year],
    }


def duplicate_groups() -> dict:
    with db() as con:
        sizes = [r[0] for r in con.execute(
            "SELECT size FROM assets WHERE missing=0 AND size>0 "
            "GROUP BY size HAVING COUNT(*)>1 ORDER BY size DESC LIMIT 100"
        ).fetchall()]
        groups = []
        for size in sizes:
            rows = con.execute(
                "SELECT * FROM assets WHERE missing=0 AND size=? ORDER BY path", (size,)
            ).fetchall()
            groups.append({"size": size, "size_human": human_size(size),
                           "items": [row_to_dict(r) for r in rows]})
    return {"groups": groups, "wasted": sum(
        g["size"] * (len(g["items"]) - 1) for g in groups)}


# --------------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "my-shelf/1.0"

    def log_message(self, fmt: str, *args) -> None:
        # 只保留错误，避免每封图片都刷一行日志。
        if self.path.startswith("/api/") and " 4" in (fmt % args)[-6:]:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ---- 基础响应 ----
    def send_bytes(self, body: bytes, status: int = 200,
                   content_type: str = "application/octet-stream",
                   extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_bytes(body, status, "application/json; charset=utf-8")

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def send_file(self, path: Path, content_type: str | None = None) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self.send_json({"error": "file not found"}, 404)
            return
        guessed = content_type or CONTENT_TYPES.get(path.suffix.casefold()) \
            or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_bytes(body, 200, guessed)

    def send_stream(self, path: str) -> None:
        """支持 HTTP Range，网页播放器才能拖动进度条。"""
        try:
            size = os.path.getsize(path)
            ext = Path(path).suffix.casefold()
            ctype = CONTENT_TYPES.get(ext, "video/mp4")
            range_header = self.headers.get("Range")
            start, end = 0, max(0, size - 1)
            status = 200
            if range_header:
                match = re.match(r"bytes=(\d*)-(\d*)", range_header.strip(), re.I)
                if match:
                    if match.group(1):
                        start = int(match.group(1))
                    if match.group(2):
                        end = min(int(match.group(2)), size - 1)
                    if start >= size or start > end:
                        self.send_bytes(b"", 416, ctype,
                                        {"Content-Range": f"bytes */{size}"})
                        return
                    status = 206
            length = end - start + 1
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.send_header("Access-Control-Allow-Origin", "*")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with open(path, "rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = handle.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except OSError as error:
            try:
                self.send_json({"error": str(error)}, 404)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def asset_path(self, asset_id: str, column: str) -> str | None:
        try:
            ident = int(asset_id)
        except ValueError:
            return None
        with db() as con:
            row = con.execute(f"SELECT {column} FROM assets WHERE id=?", (ident,)).fetchone()
        return row[column] if row and row[column] else None

    # ---- 路由 ----
    def do_OPTIONS(self) -> None:
        self.send_bytes(b"", 204, "text/plain", {
            "Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        })

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        params = parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html"):
                return self.send_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
            if path.startswith("/static/"):
                target = (WEB_DIR / path[len("/static/"):]).resolve()
                if WEB_DIR.resolve() in target.parents and target.is_file():
                    return self.send_file(target)
                return self.send_json({"error": "not found"}, 404)
            if path.startswith("/cover/"):
                cover = self.asset_path(path[len("/cover/"):], "cover")
                if not cover or Path(cover).suffix.casefold() not in IMAGE_EXTS:
                    return self.send_json({"error": "no cover"}, 404)
                return self.send_file(Path(cover))
            if path.startswith("/stream/"):
                video = self.asset_path(path[len("/stream/"):], "path")
                if not video or not Path(video).is_file():
                    return self.send_json({"error": "file not found"}, 404)
                return self.send_stream(video)
            if not path.startswith("/api/"):
                return self.send_json({"error": "not found"}, 404)

            if path == "/api/items":
                return self.send_json(query_items(params))
            if path == "/api/facets":
                return self.send_json(facets())
            if path == "/api/stats":
                return self.send_json(stats())
            if path == "/api/duplicates":
                return self.send_json(duplicate_groups())
            if path == "/api/roots":
                return self.send_json({"roots": current_roots()})
            if path == "/api/scan/status":
                with SCAN_LOCK:
                    return self.send_json(dict(SCAN))
            if path.startswith("/api/items/"):
                ident = path[len("/api/items/"):]
                with db() as con:
                    row = con.execute("SELECT * FROM assets WHERE id=?", (ident,)).fetchone()
                return self.send_json(row_to_dict(row) if row else {"error": "not found"},
                                      200 if row else 404)
            if path == "/api/export":
                data = query_items({**params, "page": ["1"], "page_size": ["1000000"]},
                                  cap=1_000_000)
                output = io.StringIO()
                writer = csv.writer(output)
                writer.writerow(["标题", "年份", "评分", "标签", "格式", "清晰度",
                                 "大小(字节)", "收藏", "已看", "路径", "简介"])
                for item in data["items"]:
                    writer.writerow([
                        item["title"], item["year"] or "", item["rating"] or "",
                        ",".join(item["tags"]), item["ext"], item["resolution"],
                        item["size"], int(item["favorite"]), int(item["watched"]),
                        item["path"], item["plot"],
                    ])
                body = ("\ufeff" + output.getvalue()).encode("utf-8")
                return self.send_bytes(body, 200, "text/csv; charset=utf-8", {
                    "Content-Disposition": 'attachment; filename="my-shelf.csv"'})
            return self.send_json({"error": "unknown api"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:  # noqa: BLE001
            traceback.print_exc()
            try:
                self.send_json({"error": str(error)}, 500)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_POST(self) -> None:
        path = unquote(urlparse(self.path).path)
        body = self.read_json()
        try:
            if path == "/api/scan":
                roots = body.get("roots")
                started = start_scan(roots if isinstance(roots, list) else None)
                return self.send_json({"started": started,
                                       "message": "已在后台开始扫描" if started else "扫描正在进行中"})
            if path == "/api/roots":
                raw = str(body.get("path") or "").strip().strip('"')
                candidate = Path(raw)
                if not raw or not candidate.is_dir():
                    return self.send_json({"error": "目录不存在：" + raw}, 400)
                resolved = str(candidate.resolve())
                with CONFIG_LOCK:
                    if resolved not in CONFIG["roots"]:
                        CONFIG["roots"].append(resolved)
                save_config()
                return self.send_json({"roots": current_roots(), "added": resolved})
            if path.startswith("/api/open/"):
                video = self.asset_path(path[len("/api/open/"):], "path")
                if not video or not Path(video).is_file():
                    return self.send_json({"error": "file not found"}, 404)
                if sys.platform == "win32":
                    os.startfile(video)  # type: ignore[attr-defined]
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", video])
                else:
                    subprocess.Popen(["xdg-open", video])
                return self.send_json({"opened": True})
            return self.send_json({"error": "unknown api"}, 404)
        except Exception as error:  # noqa: BLE001
            traceback.print_exc()
            return self.send_json({"error": str(error)}, 500)

    def do_PATCH(self) -> None:
        path = unquote(urlparse(self.path).path)
        body = self.read_json()
        try:
            if not path.startswith("/api/items/"):
                return self.send_json({"error": "unknown api"}, 404)
            ident = path[len("/api/items/"):]
            changes: dict = {}
            for key, value in body.items():
                if key not in EDITABLE_FIELDS:
                    continue
                if key == "tags":
                    if not isinstance(value, list):
                        value = [t.strip() for t in str(value).split(",") if t.strip()]
                    changes[key] = json.dumps(value, ensure_ascii=False)
                elif key in ("favorite", "watched"):
                    changes[key] = 1 if value else 0
                elif key in ("year", "rating", "progress"):
                    try:
                        changes[key] = float(value) if value not in ("", None) else None
                    except (TypeError, ValueError):
                        continue
                elif key == "cover":
                    if not value or (Path(str(value)).is_file()
                                     and Path(str(value)).suffix.casefold() in IMAGE_EXTS):
                        changes[key] = str(value or "")
                else:
                    changes[key] = str(value or "")
            if not changes:
                return self.send_json({"error": "没有可更新的字段"}, 400)
            if any(key in METADATA_FIELDS for key in changes):
                changes["edited"] = 1
            assignments = ", ".join(f"{key}=?" for key in changes)
            with db() as con:
                con.execute(f"UPDATE assets SET {assignments} WHERE id=?",
                            (*changes.values(), ident))
                row = con.execute("SELECT * FROM assets WHERE id=?", (ident,)).fetchone()
            return self.send_json(row_to_dict(row) if row else {"error": "not found"},
                                  200 if row else 404)
        except Exception as error:  # noqa: BLE001
            traceback.print_exc()
            return self.send_json({"error": str(error)}, 500)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path != "/api/roots":
                return self.send_json({"error": "unknown api"}, 404)
            target = parse_qs(parsed.query).get("path", [""])[0]
            with CONFIG_LOCK:
                CONFIG["roots"] = [r for r in CONFIG["roots"] if r != target]
            save_config()
            return self.send_json({"roots": current_roots(), "removed": target})
        except Exception as error:  # noqa: BLE001
            return self.send_json({"error": str(error)}, 500)


def main() -> None:
    parser = argparse.ArgumentParser(description="我的片库 · 本地媒体库服务")
    parser.add_argument("--root", action="append", default=[],
                        help="媒体目录，可重复指定；也可以用网页里的「添加目录」")
    parser.add_argument("--port", type=int, default=None, help="端口，默认 8765")
    parser.add_argument("--no-browser", action="store_true", help="启动时不自动打开浏览器")
    parser.add_argument("--no-scan", action="store_true", help="启动时不自动扫描")
    args = parser.parse_args()

    loaded = load_config()
    CONFIG["port"] = args.port or loaded["port"]
    CONFIG["roots"] = [str(Path(p).resolve()) for p in args.root] or loaded["roots"]
    if args.root:
        save_config()

    init_db()
    roots = current_roots()
    if roots and not args.no_scan:
        start_scan(roots)

    url = f"http://127.0.0.1:{CONFIG['port']}"
    print("=" * 58)
    print("  我的片库 my-shelf  ·  本地媒体库")
    print("=" * 58)
    print(f"  地址      {url}")
    print(f"  数据库    {DB_PATH}")
    print(f"  媒体目录  {len(roots)} 个" + ("，正在后台扫描…" if roots else "（尚未添加）"))
    if not roots:
        print("  首次使用：把媒体文件夹拖到 start.bat 上，")
        print("            或在网页里点「添加目录」填绝对路径。")
    print("  按 Ctrl+C 停止")
    print("=" * 58, flush=True)

    server = ThreadingHTTPServer(("127.0.0.1", CONFIG["port"]), Handler)
    server.daemon_threads = True
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
