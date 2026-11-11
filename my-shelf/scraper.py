"""我的片库 · 在线元数据刮削

纯 Python 标准库实现，不依赖任何第三方包。

数据源（可插拔、按配置顺序回退）：
  * wikidata  —— 默认源，零 key。结构化字段（上映年 / 导演 / 演员 / 题材 /
                 时长 / IMDb 编号）+ 中文维基百科剧情摘要与海报。
  * tmdb      —— 可选，需自备 API key；字段最全，但部分地区网络可能不通。
  * omdb      —— 可选，需自备 API key；主要用来补 IMDb 评分、简介与海报。

对外只暴露三个入口：``candidates()``（手动匹配候选）、``resolve()``（自动
匹配并取详情）、``fetch_by_id()``（按外部 ID 精确取详情）。网络错误一律
转成 ``ScrapeError``，由调用方决定是跳过还是记录失败，不会拖垮批量任务。
"""

from __future__ import annotations

import difflib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

USER_AGENT = "MyShelf/1.0 (personal local media library; +https://github.com/crazyzero945/peach)"

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
TMDB_API = "https://api.themoviedb.org/3"
TMDB_IMAGE = "https://image.tmdb.org/t/p/w500"
OMDB_API = "https://www.omdbapi.com/"

# Wikidata 属性
P_INSTANCE = "P31"
P_RELEASE = "P577"
P_DIRECTOR = "P57"
P_CAST = "P161"
P_GENRE = "P136"
P_IMDB = "P345"
P_IMAGE = "P18"
P_RUNTIME = "P2047"
P_LANGUAGE = "P364"
Q_FILM = "Q11424"

LABEL_LANGS = ("zh-cn", "zh-hans", "zh", "en")
WIKI_LANGS = ("zh", "en")

FILM_WORDS = re.compile(r"film|movie|cinema|電影|电影|影片|劇集|剧集", re.I)
POSTER_WORDS = re.compile(r"poster|cover|film|movie|海报|封面", re.I)
IMAGE_SKIP = re.compile(r"logo|icon|flag|commons|wikimedia|edit|svg|button|arrow|star", re.I)
NOISE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)


class ScrapeError(Exception):
    """刮削源不可用或未找到结果。"""


# --------------------------------------------------------------------------- HTTP

def _request(url: str, timeout: float = 12.0, max_bytes: int = 8 * 1024 * 1024) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json, image/*, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = response.read(max_bytes + 1)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as error:
        raise ScrapeError(f"网络请求失败：{error}") from error
    if len(data) > max_bytes:
        raise ScrapeError("响应过大，已放弃")
    return data


def _json(url: str, timeout: float = 12.0) -> dict:
    try:
        return json.loads(_request(url, timeout).decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError) as error:
        raise ScrapeError(f"数据源返回了无法解析的内容：{error}") from error


def fetch_image(url: str, timeout: float = 20.0) -> bytes:
    """下载海报；只接受常见图片格式，避免把 HTML 错误页当图片存盘。"""
    data = _request(url, timeout, max_bytes=6 * 1024 * 1024)
    if data.startswith(b"\xff\xd8\xff") or data.startswith(b"\x89PNG\r\n\x1a\n") \
            or data.startswith(b"RIFF") or data.startswith(b"GIF8"):
        return data
    raise ScrapeError("海报地址返回的不是图片")


# --------------------------------------------------------------------------- 工具

def normalize(text: str) -> str:
    return NOISE.sub("", (text or "").casefold())


def similarity(a: str, b: str) -> float:
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.88
    return difflib.SequenceMatcher(None, na, nb).ratio()


def _pick_label(labels: dict) -> str:
    for lang in LABEL_LANGS:
        if lang in labels and labels[lang].get("value"):
            return labels[lang]["value"]
    for value in labels.values():
        if isinstance(value, dict) and value.get("value"):
            return value["value"]
    return ""


def _pick_description(descriptions: dict) -> str:
    for lang in ("zh-cn", "zh-hans", "zh", "en"):
        if lang in descriptions and descriptions[lang].get("value"):
            return descriptions[lang]["value"]
    return ""


def _claim_values(entity: dict, prop: str, limit: int = 12) -> list:
    out = []
    for claim in entity.get("claims", {}).get(prop, [])[:limit]:
        value = claim.get("mainsnak", {}).get("datavalue", {}).get("value")
        out.append(value)
    return out


def _claim_ids(entity: dict, prop: str, limit: int = 12) -> list[str]:
    ids = []
    for value in _claim_values(entity, prop, limit):
        if isinstance(value, dict) and value.get("entity-type") == "item" and value.get("id"):
            ids.append(value["id"])
    return ids


def _year_from_claims(entity: dict) -> int | None:
    for value in _claim_values(entity, P_RELEASE, 4):
        if isinstance(value, dict) and value.get("time"):
            match = re.match(r"[+-]?(\d{4})", value["time"])
            if match:
                return int(match.group(1))
    return None


def _runtime_from_claims(entity: dict) -> int | None:
    for value in _claim_values(entity, P_RUNTIME, 2):
        if isinstance(value, dict) and value.get("amount"):
            try:
                return int(float(value["amount"]))
            except (TypeError, ValueError):
                continue
    return None


def _first_text(values: list) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


# --------------------------------------------------------------------------- Wikidata

def wd_search(query: str, language: str = "zh", limit: int = 8) -> list[dict]:
    url = WIKIDATA_API + "?" + urllib.parse.urlencode({
        "action": "wbsearchentities", "search": query, "language": language,
        "uselang": language, "format": "json", "limit": limit, "type": "item",
    })
    data = _json(url)
    return [hit for hit in data.get("search", []) if hit.get("id")]


def wd_entities(ids: list[str], props: str = "labels|descriptions|claims|sitelinks") -> dict:
    """一次最多取 50 个实体；超出自动分批。"""
    result: dict[str, Any] = {}
    for start in range(0, len(ids), 50):
        batch = ids[start:start + 50]
        if not batch:
            continue
        url = WIKIDATA_API + "?" + urllib.parse.urlencode({
            "action": "wbgetentities", "ids": "|".join(batch), "props": props,
            "languages": "zh|zh-cn|zh-hans|zh-hant|en", "format": "json",
        })
        result.update(_json(url, timeout=15).get("entities", {}))
    return result


def wd_labels(ids: list[str]) -> dict[str, str]:
    if not ids:
        return {}
    entities = wd_entities(list(dict.fromkeys(ids)), props="labels")
    return {qid: _pick_label(ent.get("labels", {}))
            for qid, ent in entities.items() if _pick_label(ent.get("labels", {}))}


def wd_score(entity: dict, title: str, year: int | None) -> int:
    labels = entity.get("labels", {})
    label = _pick_label(labels)
    desc = _pick_description(entity.get("descriptions", {}))
    score = 0
    if Q_FILM in _claim_ids(entity, P_INSTANCE, 4):
        score += 55
    if FILM_WORDS.search(desc or ""):
        score += 22
    elif FILM_WORDS.search(label or ""):
        score += 8
    candidate_year = _year_from_claims(entity)
    if year and candidate_year:
        score += 35 if abs(candidate_year - year) <= 1 else -30
    ratio = max(similarity(label, title),
                similarity(_pick_label({"en": labels.get("en", {})}), title))
    score += int(ratio * 30)
    return score


def candidates(query: str, year: int | None = None, limit: int = 8) -> list[dict]:
    """手动匹配用的候选列表；按「像不像一部电影 + 年份 + 标题」排序。"""
    hits: dict[str, dict] = {}
    terms = [query]
    if year:
        terms.append(f"{query} {year}")
    for term in terms:
        for lang in ("zh", "en"):
            try:
                found = wd_search(term, lang, limit=10)
            except ScrapeError:
                continue
            for hit in found:
                hits.setdefault(hit["id"], hit)
    if not hits:
        raise ScrapeError("没有搜到任何候选，换个关键词试试")
    entities = wd_entities(list(hits)[:30])
    ranked = []
    for qid, entity in entities.items():
        if "missing" in entity:
            continue
        label = _pick_label(entity.get("labels", {}))
        desc = _pick_description(entity.get("descriptions", {}))
        yr = _year_from_claims(entity)
        en_title = (entity.get("sitelinks", {}).get("enwiki") or {}).get("title", "")
        original = re.sub(r"\s*\([^)]*\)\s*$", "", en_title).strip()
        ranked.append({
            "qid": qid, "title": label or hits.get(qid, {}).get("label", qid),
            "original_title": original,
            "year": yr, "description": desc or hits.get(qid, {}).get("description", ""),
            "source": "wikidata", "score": wd_score(entity, query, year),
            "url": f"https://www.wikidata.org/wiki/{qid}",
        })
    ranked.sort(key=lambda x: x["score"], reverse=True)
    return ranked[:limit]


# --------------------------------------------------------------------------- Wikipedia

def wiki_page(site: str, title: str) -> dict:
    """取条目摘要与图片文件名列表。中文维基用 variant 转成简体。"""
    host = "zh" if site.startswith("zh") else "en"
    params = {
        "action": "query", "titles": title, "prop": "extracts|images",
        "exintro": 1, "explaintext": 1, "imlimit": 40, "redirects": 1,
        "format": "json",
    }
    if host == "zh":
        params["variant"] = "zh-cn"
    data = _json(f"https://{host}.wikipedia.org/w/api.php?" + urllib.parse.urlencode(params),
                 timeout=12)
    pages = data.get("query", {}).get("pages", {})
    for page in pages.values():
        if "missing" in page:
            continue
        return {"extract": (page.get("extract") or "").strip(),
                "images": [img["title"] for img in page.get("images", [])]}
    return {"extract": "", "images": []}


def choose_poster_file(images: list[str], title: str) -> str:
    scored = []
    for name in images:
        if IMAGE_SKIP.search(name):
            continue
        if not re.search(r"\.(jpe?g|png|webp)$", name, re.I):
            continue
        score = 0
        if POSTER_WORDS.search(name):
            score += 5
        if similarity(name, title) > 0.45:
            score += 3
        scored.append((score, name))
    if not scored:
        return ""
    scored.sort(key=lambda x: (-x[0], len(x[1])))
    return scored[0][1]


def wiki_image_url(file_title: str, width: int = 500) -> str:
    params = {
        "action": "query", "titles": file_title, "prop": "imageinfo",
        "iiprop": "url|mime|size", "iiurlwidth": width, "format": "json",
    }
    for site in ("zh", "en"):
        url = f"https://{site}.wikipedia.org/w/api.php?" + urllib.parse.urlencode(params)
        try:
            data = _json(url, timeout=10)
        except ScrapeError:
            continue
        for page in data.get("query", {}).get("pages", {}).values():
            info = (page.get("imageinfo") or [{}])[0]
            return info.get("thumburl") or info.get("url") or ""
    return ""


# --------------------------------------------------------------------------- 详情

def fetch_by_id(qid: str, want_poster: bool = True, plot_limit: int = 700) -> dict:
    """按 Wikidata QID 精确取详情（手动绑定时用这个）。"""
    entities = wd_entities([qid])
    entity = entities.get(qid)
    if not entity or "missing" in entity:
        raise ScrapeError(f"找不到条目 {qid}")

    labels = entity.get("labels", {})
    title = _pick_label(labels)
    # wbgetentities 的 languages 参数经常只回中文变体，英文原名改从 enwiki 条目名取，
    # 去掉 "(2021 film)" 之类的消歧义后缀，零额外请求。
    en_title = (entity.get("sitelinks", {}).get("enwiki") or {}).get("title", "")
    original = re.sub(r"\s*\([^)]*\)\s*$", "", en_title).strip() or title
    director_ids = _claim_ids(entity, P_DIRECTOR, 4)
    cast_ids = _claim_ids(entity, P_CAST, 10)
    genre_ids = _claim_ids(entity, P_GENRE, 8)
    names = wd_labels(director_ids + cast_ids + genre_ids)

    imdb = _first_text(_claim_values(entity, P_IMDB, 2))
    sitelinks = entity.get("sitelinks", {})
    wiki_title = ""
    wiki_site = ""
    for site in ("zhwiki", "zh_yuewiki", "enwiki"):
        if site in sitelinks:
            wiki_site, wiki_title = site, sitelinks[site]["title"]
            break

    plot, poster_url = "", ""
    if wiki_site and wiki_title:
        try:
            page = wiki_page(wiki_site, wiki_title)
            plot = page["extract"][:plot_limit]
            if want_poster:
                picked = choose_poster_file(page["images"], title)
                if picked:
                    poster_url = wiki_image_url(picked)
        except ScrapeError:
            pass  # 摘要 / 海报是锦上添花，拿不到不影响结构化字段

    return {
        "title": title, "original_title": original,
        "year": _year_from_claims(entity), "plot": plot,
        "genres": [names[q] for q in genre_ids if names.get(q)],
        "actors": [names[q] for q in cast_ids if names.get(q)],
        "director": "、".join(names[q] for q in director_ids if names.get(q)),
        "runtime": _runtime_from_claims(entity),
        "imdb": imdb, "external_id": qid,
        "external_url": f"https://www.wikidata.org/wiki/{qid}",
        "poster_url": poster_url,
        "source": "wikidata",
        "description": _pick_description(entity.get("descriptions", {})),
    }


def resolve_wikidata(title: str, year: int | None, want_poster: bool) -> dict:
    ranked = candidates(title, year, limit=6)
    if not ranked or ranked[0]["score"] < 45:
        raise ScrapeError("没有可信的电影匹配，建议用「手动匹配」指定条目")
    return fetch_by_id(ranked[0]["qid"], want_poster=want_poster)


# --------------------------------------------------------------------------- 可选源

def resolve_tmdb(title: str, year: int | None, api_key: str, language: str) -> dict | None:
    """TMDB 可选源：没有 key 或网络不通时快速返回 None，交给下一个源。"""
    if not api_key:
        return None
    params = {"api_key": api_key, "query": title, "language": language, "page": 1}
    if year:
        params["year"] = year
    try:
        data = _json(TMDB_API + "/search/movie?" + urllib.parse.urlencode(params), timeout=7)
        results = data.get("results") or []
        if not results:
            return None
        best = max(results, key=lambda m: similarity(m.get("title") or "", title)
                   + (0.3 if year and str(m.get("release_date", "")).startswith(str(year)) else 0))
        detail = _json(TMDB_API + f"/movie/{best['id']}?" + urllib.parse.urlencode({
            "api_key": api_key, "language": language,
            "append_to_response": "credits",
        }), timeout=7)
        credits = detail.get("credits", {})
        crew = credits.get("crew", [])
        casts = credits.get("cast", [])[:10]
        poster = best.get("poster_path") or detail.get("poster_path")
        release = detail.get("release_date") or ""
        return {
            "title": detail.get("title") or best.get("title") or title,
            "original_title": detail.get("original_title") or "",
            "year": int(release[:4]) if release[:4].isdigit() else year,
            "rating": round(float(detail.get("vote_average")), 1) if detail.get("vote_average") else None,
            "plot": detail.get("overview") or "",
            "genres": [g["name"] for g in detail.get("genres", [])],
            "actors": [c["name"] for c in casts],
            "director": "、".join(c["name"] for c in crew if c.get("job") == "Director"),
            "runtime": detail.get("runtime") or None,
            "imdb": detail.get("imdb_id") or "",
            "external_id": f"tmdb:{detail.get('id')}",
            "external_url": f"https://www.themoviedb.org/movie/{detail.get('id')}",
            "poster_url": TMDB_IMAGE + poster if poster else "",
            "source": "tmdb", "description": "",
        }
    except (ScrapeError, ValueError, TypeError, KeyError):
        return None


def enrich_omdb(meta: dict, title: str, year: int | None, api_key: str) -> dict:
    """OMDb 作为补充源：只补主源缺的评分 / 简介 / 海报。"""
    if not api_key:
        return meta
    params = {"t": title, "apikey": api_key, "plot": "short"}
    if year:
        params["y"] = year
    try:
        data = _json(OMDB_API + "?" + urllib.parse.urlencode(params), timeout=7)
        if data.get("Response") != "True":
            return meta
        if not meta.get("rating") and data.get("imdbRating") not in ("N/A", "", None):
            try:
                meta["rating"] = float(data["imdbRating"])
            except (TypeError, ValueError):
                pass
        if not meta.get("plot") and data.get("Plot") not in ("N/A", "", None):
            meta["plot"] = data["Plot"]
        if not meta.get("poster_url") and data.get("Poster") not in ("N/A", "", None):
            meta["poster_url"] = data["Poster"]
        if not meta.get("genres") and data.get("Genre") not in ("N/A", "", None):
            meta["genres"] = [g.strip() for g in data["Genre"].split(",")]
        if not meta.get("director") and data.get("Director") not in ("N/A", "", None):
            meta["director"] = data["Director"]
        if not meta.get("actors") and data.get("Actors") not in ("N/A", "", None):
            meta["actors"] = [a.strip() for a in data["Actors"].split(",")]
        meta.setdefault("omdb", True)
    except ScrapeError:
        pass
    return meta


# --------------------------------------------------------------------------- 入口

def resolve(title: str, year: int | None = None, config: dict | None = None,
            want_poster: bool = True) -> dict:
    """按配置顺序自动匹配；返回统一结构的元数据字典。"""
    cfg = config or {}
    title = (title or "").strip()
    if not title:
        raise ScrapeError("标题为空，无法刮削")
    errors: list[str] = []

    if cfg.get("tmdb_key"):
        meta = resolve_tmdb(title, year, cfg["tmdb_key"], cfg.get("language") or "zh-CN")
        if meta:
            return enrich_omdb(meta, meta.get("original_title") or title, year,
                               cfg.get("omdb_key", ""))

    provider = cfg.get("provider") or "wikidata"
    if provider in ("wikidata", "auto"):
        try:
            meta = resolve_wikidata(title, year, want_poster)
            return enrich_omdb(meta, meta.get("original_title") or title, year,
                               cfg.get("omdb_key", ""))
        except ScrapeError as error:
            errors.append(f"wikidata: {error}")
    raise ScrapeError("；".join(errors) or "没有启用任何可用的刮削源")


if __name__ == "__main__":
    import sys
    keyword = " ".join(sys.argv[1:]) or "Dune 2021"
    print("搜索候选：", keyword)
    for row in candidates(keyword):
        print(f"  {row['score']:>4}  {row['qid']:<12} {row['year'] or '—':<6} {row['title']}  {row['description'][:40]}")
    best = candidates(keyword)[0]["qid"]
    print("\n详情：")
    print(json.dumps(fetch_by_id(best), ensure_ascii=False, indent=2)[:2000])
