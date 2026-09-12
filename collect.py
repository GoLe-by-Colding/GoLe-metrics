#!/usr/bin/env python3
"""GoLe 전달 지표 수집기.

무엇을 재는지보다 **무엇을 재지 않는지**가 더 중요하다. docs/측정 원칙.md 를 먼저 읽어라.

이 스크립트는 "AI 가 우리를 빠르게 하는가"를 답하지 않는다. 그건 대조군이 없어
증명할 수 없다. 대신 **"우리가 만든 하네스가 제 역할을 하는가"** 를 답한다.
전자는 평행우주가 필요하고, 후자는 오늘 데이터로 계산된다.

의존성: python3 (표준 라이브러리만) + gh CLI (로그인되어 있어야 함).
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import pathlib
import re
import statistics
import subprocess
import sys

REPO_DEFAULT = "GoLe-by-Colding/GoLe"
REWORK_WINDOW_DAYS = 7
BOT_LOGINS = {"dependabot", "app/dependabot", "dependabot[bot]", "github-actions[bot]"}
FIX_TITLE = re.compile(r"^(fix|revert|hotfix)\b", re.I)


# ─────────────────────────────── gh 호출 ───────────────────────────────

def gh(*args: str) -> str:
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"gh {' '.join(args)} 실패:\n{r.stderr.strip()}")
    return r.stdout


def gh_json(*args: str):
    return json.loads(gh(*args) or "[]")


def git(repo_path: str, *args: str) -> str:
    r = subprocess.run(["git", "-C", repo_path, *args], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def parse_ts(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def is_bot(login: str) -> bool:
    return login in BOT_LOGINS or login.endswith("[bot]")


# ─────────────────────────────── 수집 ───────────────────────────────

def fetch_prs(repo: str, limit: int) -> list[dict]:
    """머지된 PR. 봇 PR 은 따로 센다 — 사람 작업과 성격이 다르다."""
    fields = "number,title,author,createdAt,mergedAt,additions,deletions,files,reviews,baseRefName,headRefName"
    return gh_json("pr", "list", "--repo", repo, "--state", "merged",
                   "--limit", str(limit), "--json", fields)


def fetch_ci_runs(repo: str, limit: int) -> list[dict]:
    fields = "databaseId,conclusion,headBranch,headSha,createdAt,event,workflowName"
    return gh_json("run", "list", "--repo", repo, "--workflow", "ci.yml",
                   "--limit", str(limit), "--json", fields)


# ─────────────────────────────── 지표 ───────────────────────────────

def metric_rework(prs: list[dict]) -> dict:
    """재작업률 — 머지 후 7일 안에 같은 파일을 fix/revert 가 다시 건드린 PR 비율.

    한계: 같은 파일을 건드렸다고 반드시 앞 PR 때문은 아니다. 기능을 이어서
    만드느라 건드린 것도 잡힌다. 절대값보다 **추세**로 봐라.
    """
    human = [p for p in prs if not is_bot(p["author"]["login"]) and p["mergedAt"]]
    human.sort(key=lambda p: p["mergedAt"])

    reworked, detail = 0, []
    for i, pr in enumerate(human):
        merged = parse_ts(pr["mergedAt"])
        my_files = {f["path"] for f in (pr.get("files") or [])}
        if not my_files:
            continue
        hits = []
        for later in human[i + 1:]:
            lt = parse_ts(later["mergedAt"])
            if (lt - merged).days > REWORK_WINDOW_DAYS:
                break
            if not FIX_TITLE.match(later["title"]):
                continue
            overlap = my_files & {f["path"] for f in (later.get("files") or [])}
            if overlap:
                hits.append({"pr": later["number"], "title": later["title"],
                             "overlap": sorted(overlap)[:5]})
        if hits:
            reworked += 1
            detail.append({"pr": pr["number"], "title": pr["title"], "followed_by": hits})

    n = len([p for p in human if p.get("files")])
    return {
        "설명": f"머지 후 {REWORK_WINDOW_DAYS}일 내 같은 파일을 fix/revert 가 다시 건드린 PR 비율",
        "대상_PR수": n,
        "재작업_PR수": reworked,
        "재작업률": round(reworked / n * 100, 1) if n else None,
        "사례": detail[:10],
    }


def metric_ci_first_try(prs: list[dict], runs: list[dict]) -> dict:
    """CI 첫 시도 통과율 — 브랜치의 첫 CI 실행이 바로 성공했는가.

    에이전트가 올린 첫 결과물이 그대로 서는 비율에 가장 가깝다.
    한계: cancelled 는 사람이 끊었거나 새 푸시로 취소된 것이라 제외한다.
    """
    by_branch: dict[str, list[dict]] = collections.defaultdict(list)
    for r in runs:
        if r["conclusion"] in ("success", "failure"):
            by_branch[r["headBranch"]].append(r)

    pr_branches = {p["headRefName"] for p in prs}
    first_ok = first_total = 0
    attempts = []
    for br, rs in by_branch.items():
        if br in ("main", "dev") or br not in pr_branches:
            continue
        rs.sort(key=lambda r: r["createdAt"])
        first_total += 1
        if rs[0]["conclusion"] == "success":
            first_ok += 1
        attempts.append(len(rs))

    return {
        "설명": "PR 브랜치의 첫 CI 실행이 바로 성공한 비율 (main·dev 직접 푸시 제외)",
        "대상_브랜치수": first_total,
        "첫시도_성공": first_ok,
        "첫시도_통과율": round(first_ok / first_total * 100, 1) if first_total else None,
        "브랜치당_CI실행_중앙값": statistics.median(attempts) if attempts else None,
        "브랜치당_CI실행_최대": max(attempts) if attempts else None,
    }


def metric_ci_overall(runs: list[dict]) -> dict:
    c = collections.Counter(r["conclusion"] for r in runs)
    ok, fail = c.get("success", 0), c.get("failure", 0)
    return {
        "설명": "ci.yml 전체 실행 결과 분포 (최근 N건)",
        "분포": dict(c),
        "성공률": round(ok / (ok + fail) * 100, 1) if ok + fail else None,
    }


def metric_lead_time(prs: list[dict]) -> dict:
    """머지까지 걸린 시간. 사람 PR 만 센다."""
    hours = []
    for p in prs:
        if is_bot(p["author"]["login"]) or not p["mergedAt"]:
            continue
        c, m = parse_ts(p["createdAt"]), parse_ts(p["mergedAt"])
        hours.append((m - c).total_seconds() / 3600)
    if not hours:
        return {"설명": "PR 열림 → 머지까지 걸린 시간", "표본": 0}
    hours.sort()
    return {
        "설명": "PR 열림 → 머지까지 걸린 시간 (사람 PR 만)",
        "표본": len(hours),
        "중앙값_시간": round(statistics.median(hours), 1),
        "p90_시간": round(hours[int(len(hours) * 0.9)], 1),
        "최대_시간": round(hours[-1], 1),
    }


def metric_pr_size(prs: list[dict]) -> dict:
    sizes = [p["additions"] + p["deletions"] for p in prs
             if not is_bot(p["author"]["login"])]
    if not sizes:
        return {"설명": "PR 변경 라인 수", "표본": 0}
    sizes.sort()
    big = [p["number"] for p in prs
           if not is_bot(p["author"]["login"]) and p["additions"] + p["deletions"] > 1000]
    return {
        "설명": "PR 변경 라인 수 (추가+삭제, 사람 PR 만)",
        "표본": len(sizes),
        "중앙값": sizes[len(sizes) // 2],
        "p90": sizes[int(len(sizes) * 0.9)],
        "최대": sizes[-1],
        "1000줄_초과_PR": big,
        "주의": "큰 PR 은 검토가 사실상 불가능해진다. 에이전트는 큰 PR 을 쉽게 만든다.",
    }


def metric_review(prs: list[dict]) -> dict:
    """리뷰가 달린 PR 비율.

    GitHub 의 공식 리뷰만 센다. 오르카·대화·페어링으로 본 것은 여기 안 잡힌다.
    0 이 곧 '아무도 안 봤다'는 뜻은 아니지만, '기록이 남지 않았다'는 뜻이기는 하다.
    """
    human = [p for p in prs if not is_bot(p["author"]["login"])]
    with_review = [p["number"] for p in human if p.get("reviews")]
    return {
        "설명": "GitHub 공식 리뷰가 달린 PR 비율 (사람 PR 만)",
        "대상_PR수": len(human),
        "리뷰_있음": len(with_review),
        "리뷰율": round(len(with_review) / len(human) * 100, 1) if human else None,
        "주의": "오르카·대화로 검토한 것은 잡히지 않는다. 검토 여부가 아니라 기록 여부다.",
    }


def metric_bot_share(prs: list[dict]) -> dict:
    bot = [p for p in prs if is_bot(p["author"]["login"])]
    return {
        "설명": "전체 머지 PR 중 봇(dependabot) 비율",
        "전체": len(prs),
        "봇": len(bot),
        "봇_비율": round(len(bot) / len(prs) * 100, 1) if prs else None,
    }


def metric_by_author(prs: list[dict]) -> dict:
    """개인별 분해. 기본 리포트에는 넣지 않는다 — --by-author 로만 본다."""
    c = collections.Counter(p["author"]["login"] for p in prs
                            if not is_bot(p["author"]["login"]))
    return dict(c)


# ─────────────────────────────── 출력 ───────────────────────────────

def build_report(snap: dict) -> str:
    m = snap["지표"]
    L = [
        f"# 전달 지표 스냅샷 — {snap['수집일']}",
        "",
        f"대상: `{snap['저장소']}` · PR {snap['표본']['PR']}건 · CI 실행 {snap['표본']['CI실행']}건",
        "",
        "> 이 숫자들은 \"AI 가 우리를 빠르게 하는가\"를 답하지 않는다. 대조군이 없어 답할 수 없다.",
        "> 답하는 것은 **\"하네스가 제 역할을 하는가\"** 이고, 절대값이 아니라 **추세**로 읽어야 한다.",
        "",
        "## 한눈에",
        "",
        "| 지표 | 값 | 읽는 법 |",
        "|---|---:|---|",
    ]
    rows = [
        ("재작업률", f"{m['재작업']['재작업률']}%",
         f"머지 후 {REWORK_WINDOW_DAYS}일 내 같은 파일을 fix/revert 가 다시 건드림. 낮을수록 좋다"),
        ("CI 첫 시도 통과율", f"{m['CI_첫시도']['첫시도_통과율']}%",
         "에이전트가 올린 첫 결과물이 그대로 서는 비율. 높을수록 좋다"),
        ("브랜치당 CI 실행 (중앙값)", f"{m['CI_첫시도']['브랜치당_CI실행_중앙값']}회",
         "1에 가까울수록 좋다. 크면 CI 가 사람 검토를 대신 받아내고 있다"),
        ("CI 전체 성공률", f"{m['CI_전체']['성공률']}%", "참고용. 재실행이 섞여 낙관적으로 나온다"),
        ("머지까지 (중앙값)", f"{m['리드타임']['중앙값_시간']}h", "짧다고 좋은 게 아니다. 리뷰율과 같이 봐라"),
        ("PR 크기 (중앙값)", f"{m['PR크기']['중앙값']}줄", "p90 과 최대를 같이 봐라. 큰 PR 은 검토가 불가능해진다"),
        ("리뷰율", f"{m['리뷰']['리뷰율']}%", "GitHub 공식 리뷰만. 낮으면 검토 비용이 rework 로 미뤄진다"),
        ("봇 PR 비율", f"{m['봇비중']['봇_비율']}%", "봇이 흐름의 얼마를 차지하는가"),
    ]
    for name, val, how in rows:
        L.append(f"| {name} | {val} | {how} |")

    L += ["", "## 세부", ""]
    for key, val in m.items():
        L.append(f"### {key}")
        L.append("")
        L.append("```json")
        L.append(json.dumps(val, ensure_ascii=False, indent=2))
        L.append("```")
        L.append("")

    L += [
        "## 이번 스냅샷에서 눈여겨볼 것",
        "",
        "- **리뷰율과 재작업률은 같이 봐야 한다.** 리뷰를 건너뛰면 리드타임은 짧아지지만",
        "  그 비용이 사라지지 않고 재작업으로 나온다. 둘 중 하나만 좋아지면 의심해라.",
        "- **브랜치당 CI 실행 횟수가 크면** 에이전트가 CI 를 시행착오 도구로 쓰고 있다는 뜻이다.",
        "  나쁜 건 아니지만, 사람 검토를 대신하고 있다면 CI 가 못 잡는 종류의 결함은 그대로 나간다.",
        "- **PR 크기 p90 이 크면** 검토가 형식적으로 변한다. 에이전트는 큰 PR 을 쉽게 만든다.",
        "",
        "## 재현",
        "",
        "```bash",
        "python3 collect.py            # 리포트와 JSON 스냅샷 생성",
        "python3 collect.py --by-author  # 개인별 분해 (커밋하지 않는다)",
        "```",
    ]
    return "\n".join(L) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="GoLe 전달 지표 수집")
    ap.add_argument("--repo", default=REPO_DEFAULT)
    ap.add_argument("--pr-limit", type=int, default=200)
    ap.add_argument("--run-limit", type=int, default=300)
    ap.add_argument("--by-author", action="store_true",
                    help="개인별 분해를 화면에만 출력한다 (파일로 저장하지 않는다)")
    ap.add_argument("--out", default=".", help="저장소 루트")
    args = ap.parse_args()

    print(f"수집 중: {args.repo}", file=sys.stderr)
    prs = fetch_prs(args.repo, args.pr_limit)
    runs = fetch_ci_runs(args.repo, args.run_limit)
    print(f"  PR {len(prs)}건 · CI 실행 {len(runs)}건", file=sys.stderr)

    today = dt.date.today().isoformat()
    snap = {
        "수집일": today,
        "저장소": args.repo,
        "표본": {"PR": len(prs), "CI실행": len(runs)},
        "지표": {
            "재작업": metric_rework(prs),
            "CI_첫시도": metric_ci_first_try(prs, runs),
            "CI_전체": metric_ci_overall(runs),
            "리드타임": metric_lead_time(prs),
            "PR크기": metric_pr_size(prs),
            "리뷰": metric_review(prs),
            "봇비중": metric_bot_share(prs),
        },
    }

    root = pathlib.Path(args.out)
    (root / "data").mkdir(exist_ok=True)
    (root / "reports").mkdir(exist_ok=True)
    dpath = root / "data" / f"{today}.json"
    rpath = root / "reports" / f"{today}.md"
    dpath.write_text(json.dumps(snap, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rpath.write_text(build_report(snap), encoding="utf-8")
    print(f"  → {dpath}\n  → {rpath}", file=sys.stderr)

    if args.by_author:
        print("\n개인별 (커밋하지 않는다):", file=sys.stderr)
        for k, v in sorted(metric_by_author(prs).items(), key=lambda x: -x[1]):
            print(f"  {k:20} {v}건", file=sys.stderr)


if __name__ == "__main__":
    main()
