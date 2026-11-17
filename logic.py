"""시험 로직 (화면과 무관한 계산만 담당).

점수·합격 판정, 같은 사람 판단, 종합 판정, 순위 계산을 순수 함수로 모아 두었다.
app.py(화면)와 tests/(자동 테스트)가 함께 사용한다.
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata
from typing import Iterable

PASS_SCORE = 80   # 합격 기준 점수 (응시 1회, 카테고리별 종합 판정 모두 같은 기준)
ALL = "전체"       # 전체 응시(30문항) 결과의 category 값
RANK_TOP = 10     # 순위 화면에 표시할 인원


# ---------- 점수 ----------
def calc_score(correct: int, total: int) -> int:
    """정답 수를 100점 만점으로 환산 (반올림, 브라우저 버전 Math.round와 같은 결과)."""
    if total <= 0:
        return 0
    return int(correct * 100 / total + 0.5)


def is_pass(score: int) -> bool:
    return score >= PASS_SCORE


# ---------- 같은 사람 판단 ----------
# 업체명 + 소속 + 성명이 모두 같아야 같은 사람. 띄어쓰기·문장부호·법인 표기 차이는 무시한다.
_CORP = re.compile(r"\(주\)|㈜|주식회사|\(유\)|유한회사|\(합\)|합자회사")
_PUNCT = re.compile(r"[\s·.,\-_()（）]")


def normalize_name(s: object) -> str:
    text = unicodedata.normalize("NFC", str(s or ""))
    return _PUNCT.sub("", _CORP.sub("", text)).lower()


def person_key(company: object, team: object, name: object) -> str:
    return "|".join(normalize_name(x) for x in (company, team, name))


def record_key(r: dict) -> str:
    return person_key(r.get("company"), r.get("team", ""), r.get("name"))


def same_person(r: dict, user: dict) -> bool:
    return record_key(r) == person_key(user.get("company"), user.get("team"), user.get("name"))


def person_label(p: dict) -> str:
    team = p.get("team") or ""
    return f'{p.get("company", "")}{" " + team if team else ""} · {p.get("name", "")}'


# ---------- 결과 기록 ----------
def is_valid_record(r: object) -> bool:
    return (
        isinstance(r, dict)
        and isinstance(r.get("company"), str)
        and isinstance(r.get("name"), str)
        and isinstance(r.get("category"), str)
        and isinstance(r.get("score"), (int, float))
        and not isinstance(r.get("score"), bool)
    )


def make_record(user: dict, category: str, question_ids: list[str], answers: list,
                answer_of: dict[str, int], duration_sec: int, submitted_at: str) -> dict:
    """풀이 결과를 저장용 기록(PRD 7.2 구조)으로 만든다."""
    wrong = [qid for qid, a in zip(question_ids, answers) if a != answer_of[qid]]
    total = len(question_ids)
    correct = total - len(wrong)
    return {
        "company": user["company"],
        "team": user.get("team", ""),
        "name": user["name"],
        "category": category,
        "score": calc_score(correct, total),
        "correct": correct,
        "total": total,
        "duration_sec": max(1, int(duration_sec)),
        "wrong_ids": wrong,
        "question_ids": list(question_ids),
        "submitted_at": submitted_at,
    }


# ---------- 종합 판정 ----------
def category_scores_of(r: dict, cat_of: dict[str, str]) -> dict[str, int]:
    """기록 1건의 카테고리별 점수. 전체 응시는 그 안의 카테고리별 정답 수로 환산한다."""
    if r.get("category") != ALL:
        return {r["category"]: r["score"]}
    wrong = set(r.get("wrong_ids") or [])
    tally: dict[str, list[int]] = {}
    for qid in r.get("question_ids") or []:
        cat = cat_of.get(qid)
        if cat is None:  # 문항이 바뀌어 사라진 id
            continue
        t = tally.setdefault(cat, [0, 0])
        t[0] += 1
        if qid not in wrong:
            t[1] += 1
    return {cat: calc_score(ok, n) for cat, (n, ok) in tally.items()}


def overall_of(records: Iterable[dict], categories: list[str], cat_of: dict[str, str]) -> dict:
    """종합 판정. 6개 카테고리 최고 점수가 모두 합격 기준 이상이면 status='pass'.

    status: 'pass' 종합 합격 / 'fail' 모두 응시했으나 미달 있음 / 'progress' 미응시 있음
    """
    best: dict[str, int | None] = {c: None for c in categories}
    for r in records:
        for cat, score in category_scores_of(r, cat_of).items():
            if cat in best and (best[cat] is None or score > best[cat]):
                best[cat] = score
    passed = [c for c in categories if best[c] is not None and is_pass(best[c])]
    failed = [c for c in categories if best[c] is not None and not is_pass(best[c])]
    untaken = [c for c in categories if best[c] is None]
    status = "pass" if len(passed) == len(categories) else ("progress" if untaken else "fail")
    return {"status": status, "best": best, "passed": passed, "failed": failed, "untaken": untaken}


def overall_title(o: dict, n: int) -> str:
    if o["status"] == "pass":
        return "✔ 종합 합격"
    if o["status"] == "fail":
        return f'✖ 종합 불합격 · 합격 {len(o["passed"])}/{n}'
    return f'종합 판정: 진행 중 · 합격 {len(o["passed"])}/{n}'


def overall_detail(o: dict, n: int) -> str:
    if o["status"] == "pass":
        return f"{n}개 카테고리 모두 {PASS_SCORE}점 이상입니다."
    parts = []
    if o["failed"]:
        parts.append("재응시 필요: " + ", ".join(f'{c}({o["best"][c]}점)' for c in o["failed"]))
    if o["untaken"]:
        parts.append("미응시: " + ", ".join(o["untaken"]))
    return " · ".join(parts)


# ---------- 순위 ----------
def rank_order(r: dict) -> tuple:
    """정렬 기준: 점수 높음 → 소요 시간 짧음 → 먼저 응시."""
    return (-r["score"], r.get("duration_sec", 0), str(r.get("submitted_at", "")))


def build_ranking(results: Iterable[dict], category: str) -> list[dict]:
    """카테고리별 순위. 같은 사람은 최고 기록 1건만 반영."""
    best: dict[str, dict] = {}
    for r in results:
        if r.get("category") != category:
            continue
        k = record_key(r)
        if k not in best or rank_order(r) < rank_order(best[k]):
            best[k] = r
    return sorted(best.values(), key=rank_order)


# ---------- 관리자 ----------
def wrong_rates(records: Iterable[dict], questions: list[dict]) -> list[dict]:
    stats = {q["id"]: {"id": q["id"], "category": q["category"], "asked": 0, "wrong": 0} for q in questions}
    for r in records:
        for qid in r.get("question_ids") or []:
            if qid in stats:
                stats[qid]["asked"] += 1
        for qid in r.get("wrong_ids") or []:
            if qid in stats:
                stats[qid]["wrong"] += 1
    rows = list(stats.values())
    rows.sort(key=lambda s: (-(s["wrong"] / s["asked"]) if s["asked"] else 1, s["id"]))
    return rows


def _csv_cell(value: object) -> str:
    s = "" if value is None else str(value)
    if s[:1] in ("=", "+", "-", "@", "\t", "\r"):  # 엑셀 수식 실행 방지
        s = "'" + s
    return s


def results_csv(records: Iterable[dict]) -> bytes:
    """응시 결과 CSV (엑셀 한글 깨짐 방지를 위해 UTF-8 BOM)."""
    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    w.writerow(["응시일시", "업체명", "소속", "성명", "카테고리", "점수", "정답수", "문항수", "합격여부", "소요시간(초)", "오답문항"])
    for r in records:
        w.writerow([_csv_cell(x) for x in (
            str(r.get("submitted_at", ""))[:16].replace("T", " "), r.get("company"), r.get("team", ""), r.get("name"),
            r.get("category"), r.get("score"), r.get("correct"), r.get("total"),
            "합격" if is_pass(r.get("score", 0)) else "불합격", r.get("duration_sec"), " ".join(r.get("wrong_ids") or []),
        )])
    return buf.getvalue().encode("utf-8-sig")
