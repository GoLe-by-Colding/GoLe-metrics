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


def fetch_releases(repo: str, limit: int = 100) -> list[dict]:
    return gh_json("release", "list", "--repo", repo,
                   "--limit", str(limit), "--json", "tagName,publishedAt,isDraft")


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


def metric_escaped_defects(vault: str | None, releases: list[dict]) -> dict:
    """빠져나간 결함 — 운영에서 난 문제가 릴리스당 몇 건인가.

    출처는 볼트의 `07_이슈기록/YYYY-MM-DD_제목.md` 다. **자동으로 완전히 셀 수 없다.**
    이슈가 날 때마다 사람이 기록해야 숫자가 된다.

    0 이 나왔다면 "결함이 없다"가 아니라 **"기록하지 않았다"** 일 수 있다.
    그 구분은 이 스크립트가 못 한다 — 그래서 `기록_신뢰도` 를 같이 내보낸다.
    """
    live = [r for r in releases if not r.get("isDraft")]
    base = {
        "설명": "볼트 07_이슈기록 에 쌓인 운영 이슈 수 / 릴리스 수",
        "릴리스_수": len(live),
        "주의": "자동으로 셀 수 없다. 이슈가 날 때마다 기록해야 지표가 된다. "
                "0 은 '결함이 없다'가 아니라 '기록하지 않았다'일 수 있다.",
    }

    if not vault:
        return {**base, "이슈_수": None,
                "기록_신뢰도": "볼트 경로를 못 찾음 — --vault 로 지정해라"}

    d = os.path.join(vault, "07_이슈기록")
    if not os.path.isdir(d):
        return {**base, "이슈_수": None,
                "기록_신뢰도": f"{d} 가 없다 — 볼트 경로나 폴더명을 확인해라"}

    issues = []
    for f in sorted(os.listdir(d)):
        if not f.endswith(".md") or f == "README.md":
            continue
        m = re.match(r"(\d{4}-\d{2}-\d{2})[_ ]", f)
        issues.append({"파일": f, "날짜": m.group(1) if m else None})

    n_rel = len(live)
    confidence = ("표본이 너무 작다 — 추세로 읽을 수 없다"
                  if len(issues) < 5 else "누적 중")
    return {
        **base,
        "이슈_수": len(issues),
        "릴리스당_이슈": round(len(issues) / n_rel, 2) if n_rel else None,
        "기록_신뢰도": confidence,
        "목록": issues,
    }


def metric_release_cadence(releases: list[dict]) -> dict:
    """릴리스 빈도. main 푸시 → 태그가 자동이므로 릴리스 수 = main 릴리스 횟수다."""
    live = sorted((r for r in releases if not r.get("isDraft")),
                  key=lambda r: r["publishedAt"] or "")
    if len(live) < 2:
        return {"설명": "릴리스 간격", "릴리스_수": len(live),
                "주의": "릴리스가 2건 미만이라 간격을 낼 수 없다"}
    gaps = []
    for a, b in zip(live, live[1:]):
        ta, tb = parse_ts(a["publishedAt"]), parse_ts(b["publishedAt"])
        gaps.append((tb - ta).total_seconds() / 86400)
    return {
        "설명": "릴리스 간격 (일). CalVer 태그는 main 푸시 + CI 성공 시 자동 생성된다",
        "릴리스_수": len(live),
        "첫_릴리스": live[0]["tagName"],
        "최근_릴리스": live[-1]["tagName"],
        "간격_중앙값_일": round(statistics.median(gaps), 2),
        "주의": "CD 게이트가 닫혀 있어 태그는 '릴리스했다'일 뿐 '배포됐다'가 아니다.",
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
    def fmt(v, unit=""):
        """표본이 모자라 계산이 안 된 칸은 숫자인 척하지 않는다."""
        return "—" if v is None else f"{v}{unit}"

    rows = [
        ("재작업률", fmt(m['재작업']['재작업률'], "%"),
         f"머지 후 {REWORK_WINDOW_DAYS}일 내 같은 파일을 fix/revert 가 다시 건드림. 낮을수록 좋다"),
        ("CI 첫 시도 통과율", fmt(m['CI_첫시도']['첫시도_통과율'], "%"),
         "에이전트가 올린 첫 결과물이 그대로 서는 비율. 높을수록 좋다"),
        ("브랜치당 CI 실행 (중앙값)", fmt(m['CI_첫시도']['브랜치당_CI실행_중앙값'], "회"),
         "1에 가까울수록 좋다. 크면 CI 가 사람 검토를 대신 받아내고 있다"),
        ("CI 전체 성공률", fmt(m['CI_전체']['성공률'], "%"), "참고용. 재실행이 섞여 낙관적으로 나온다"),
        ("머지까지 (중앙값)", fmt(m['리드타임'].get('중앙값_시간'), "h"), "짧다고 좋은 게 아니다. 리뷰율과 같이 봐라"),
        ("PR 크기 (중앙값)", fmt(m['PR크기'].get('중앙값'), "줄"), "p90 과 최대를 같이 봐라. 큰 PR 은 검토가 불가능해진다"),
        ("리뷰율", fmt(m['리뷰']['리뷰율'], "%"), "GitHub 공식 리뷰만. 낮으면 검토 비용이 rework 로 미뤄진다"),
        ("봇 PR 비율", fmt(m['봇비중']['봇_비율'], "%"), "봇이 흐름의 얼마를 차지하는가"),
        ("릴리스 간격 (중앙값)", fmt(m['릴리스빈도'].get('간격_중앙값_일'), "일"),
         "태그는 '릴리스했다'일 뿐 '배포됐다'가 아니다 — CD 게이트가 닫혀 있다"),
        ("릴리스당 빠져나간 결함", fmt(m['빠져나간결함'].get('릴리스당_이슈')),
         "볼트 07_이슈기록 기준. 0 은 '결함 없음'이 아니라 '기록 안 함'일 수 있다"),
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
    ap.add_argument("--vault", default=None,
                    help="옵시디언 볼트 경로 (빠져나간 결함 집계용). "
                         "생략하면 ../GoLe-obsidian 을 찾아본다")
    args = ap.parse_args()

    vault = args.vault
    if vault is None:
        guess = os.path.join(os.path.dirname(os.path.abspath(args.out)), "GoLe-obsidian")
        vault = guess if os.path.isdir(guess) else None

    print(f"수집 중: {args.repo}", file=sys.stderr)
    prs = fetch_prs(args.repo, args.pr_limit)
    runs = fetch_ci_runs(args.repo, args.run_limit)
    releases = fetch_releases(args.repo)
    print(f"  PR {len(prs)}건 · CI 실행 {len(runs)}건 · 릴리스 {len(releases)}건", file=sys.stderr)
    print(f"  볼트: {vault or '못 찾음 (빠져나간 결함은 비워 둔다)'}", file=sys.stderr)

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
            "릴리스빈도": metric_release_cadence(releases),
            "빠져나간결함": metric_escaped_defects(vault, releases),
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
