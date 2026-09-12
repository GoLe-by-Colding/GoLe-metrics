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
# 투입 대조를 '읽을 수 있다'고 말하기 위한 최소 표본. 넘겨도 통계적 검정은 아니고,
# 그 아래면 중앙값이 사실상 한두 건에 좌우되므로 판정 자체를 보류한다.
MIN_COMPARE_N = 8
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
        # 사례는 10건만 보여주지만, 투입 비교(metric_effort_vs_rework)는
        # 전수가 필요하므로 번호만 따로 싣는다.
        "재작업_PR목록": [d["pr"] for d in detail],
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


# ─────────────────────────── 에이전트 투입 ───────────────────────────
#
# 여기까지의 지표는 전부 **비율**이다 — 재작업률, 통과율, 리뷰율. 품질은 보이는데
# "얼마가 들었나"가 없다. 효율 = 산출 ÷ 투입인데 분모가 통째로 비어 있었다.
#
# 그 분모는 새로 계측할 필요가 없다. 에이전트 CLI 가 이미 로컬에 세션 로그를
# 남기고 있고, 거기에 타임스탬프·토큰·브랜치가 다 찍혀 있다.

SESSION_IDLE_GAP = dt.timedelta(minutes=15)
CLAUDE_SESSION_ROOT = "~/.claude/projects"
CODEX_SESSION_ROOT = "~/.codex/sessions"


def _iter_jsonl(path: pathlib.Path):
    """한 줄씩 흘려 읽는다. 깨진 줄은 건너뛴다 — 세션이 중간에 끊기면 생긴다."""
    try:
        with path.open(encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if not line.startswith("{"):
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def _session_files(root: pathlib.Path) -> list[pathlib.Path]:
    """세션 파일 목록. **sessionId 가 같은 파일은 하나만 남긴다.**

    작업 디렉터리를 옮기면 CLI 가 세션 이력을 새 프로젝트 폴더로 복사하는데,
    옛 폴더의 사본이 그대로 남는다. GoLe 을 `gole-project/` 아래로 옮겼을 때
    실제로 14개 세션이 바이트까지 똑같이 두 벌 생겼고, 그대로 세면 그 세션들이
    **정확히 두 배로** 계산된다.

    같은 id 가 여럿이면 가장 큰 파일을 택한다 — 복사 도중 잘린 쪽을 피한다.
    """
    best: dict[str, pathlib.Path] = {}
    for f in root.rglob("*.jsonl"):
        cur = best.get(f.stem)
        try:
            if cur is None or f.stat().st_size > cur.stat().st_size:
                best[f.stem] = f
        except OSError:
            continue
    return sorted(best.values())


def _in_repo(cwd: str | None, repo_name: str) -> bool:
    """세션의 작업 디렉터리가 이 저장소 안인가.

    경로 **세그먼트**로 맞춘다 — `GoLe` 는 잡고 `GoLe-obsidian`·`GoLe-metrics` 는
    안 잡는다. 단순 문자열 포함으로 하면 옆 저장소가 통째로 섞여 들어온다.

    예외 하나: 오르카 워크트리(`orca-workspaces/GoLe-quahog`)는 같은 저장소의
    다른 체크아웃이므로 포함한다. 팀이 전원 오르카를 쓴다.
    """
    if not cwd:
        return False
    parts = pathlib.PurePath(cwd).parts
    if repo_name in parts:
        return True
    return any(i and parts[i - 1] == "orca-workspaces" and p.startswith(repo_name + "-")
               for i, p in enumerate(parts))


def _active_seconds(times: list[dt.datetime]) -> float:
    """이벤트 타임스탬프에서 '실제로 일한 시간'을 센다.

    15분 넘게 비면 자리를 비웠거나 다른 일을 한 것으로 보고 빼낸다.
    **임계값은 임의다.** 바꾸면 숫자가 통째로 움직이므로 절대값이 아니라
    추세로만 읽어라. 세션을 켜 두고 딴짓한 시간은 어차피 못 걸러낸다.
    """
    times.sort()
    return sum((b - a).total_seconds()
               for a, b in zip(times, times[1:]) if (b - a) < SESSION_IDLE_GAP)


def _active_by_key(events: list[tuple]) -> dict[str, float]:
    """한 세션의 (시각, 키) 목록에서 키별 활성 시간을 낸다.

    **반드시 세션 전체를 한 줄로 세워 놓고 간격을 재야 한다.** 키별로 따로
    재면 같은 시간이 여러 키에 중복으로 잡힌다 — 브랜치를 A→B→A 로 오갈 때
    A 의 두 이벤트 사이에 낀 B 의 작업 시간이 A 에도 통째로 더해진다.
    (처음에 그렇게 짰다가 합계가 86시간으로 부풀었다.)

    간격은 **앞 이벤트의 키**에 준다 — 그때 돌아가고 있던 일이 그쪽이다.
    """
    events.sort(key=lambda e: e[0])
    out: dict[str, float] = collections.defaultdict(float)
    for (ta, ka), (tb, _) in zip(events, events[1:]):
        gap = tb - ta
        if gap < SESSION_IDLE_GAP:
            out[ka] += gap.total_seconds()
    for _, k in events:
        out.setdefault(k, 0.0)
    return dict(out)


def scan_claude_sessions(root: pathlib.Path, repo_name: str) -> list[dict]:
    """Claude Code 세션 로그 → 세션×브랜치 단위 투입.

    이 로그만 `gitBranch` 와 `pr-link` 를 들고 있어서 **PR 단위 귀속이 된다.**
    프롬프트·응답 본문은 읽지 않는다 — 타임스탬프·토큰·브랜치·PR 번호만 본다.
    """
    blocks = []
    for f in _session_files(root):
        in_repo = False
        linked: set[int] = set()
        events: list[tuple] = []
        span: dict[str, list[dt.datetime]] = collections.defaultdict(list)
        tokens: collections.Counter = collections.Counter()
        replies: collections.Counter = collections.Counter()

        for rec in _iter_jsonl(f):
            if not in_repo and _in_repo(rec.get("cwd"), repo_name):
                in_repo = True
            if rec.get("type") == "pr-link" and rec.get("prNumber"):
                linked.add(int(rec["prNumber"]))
            ts = parse_ts(rec.get("timestamp"))
            if ts is None:
                continue
            branch = rec.get("gitBranch") or ""
            events.append((ts, branch))
            span[branch].append(ts)
            if rec.get("type") == "assistant":
                usage = (rec.get("message") or {}).get("usage") or {}
                tokens[branch] += usage.get("output_tokens", 0)
                replies[branch] += 1

        if not in_repo or not events:
            continue
        for branch, seconds in _active_by_key(events).items():
            blocks.append({
                "도구": "claude-code",
                "세션": f.stem,
                "브랜치": branch,
                "PR링크": sorted(linked),
                "초": seconds,
                "출력토큰": tokens[branch],
                "응답": replies[branch],
                "처음": min(span[branch]),
                "마지막": max(span[branch]),
            })
    return blocks


def scan_codex_sessions(root: pathlib.Path, repo_name: str) -> list[dict]:
    """Codex 세션 로그 → 세션 단위 투입.

    Codex 는 `cwd` 만 남기고 브랜치를 안 남긴다. 그래서 **저장소 단위까지만**
    귀속되고 PR 에는 못 붙는다. 총 투입에는 들어가지만 PR 커버리지에는 안 들어간다.

    토큰은 `token_count` 이벤트의 `total_token_usage` 가 **누적값**이라
    마지막 것만 쓴다. 합치면 중복 계산된다.
    """
    blocks = []
    for f in _session_files(root):
        in_repo = False
        times: list[dt.datetime] = []
        out_tokens = 0
        replies = 0
        for rec in _iter_jsonl(f):
            payload = rec.get("payload") or {}
            if not in_repo and _in_repo(payload.get("cwd"), repo_name):
                in_repo = True
            ts = parse_ts(rec.get("timestamp"))
            if ts is not None:
                times.append(ts)
            if payload.get("type") == "token_count":
                usage = (payload.get("info") or {}).get("total_token_usage") or {}
                out_tokens = max(out_tokens, usage.get("output_tokens", 0))
            elif payload.get("type") == "agent_message":
                replies += 1
        if in_repo and times:
            blocks.append({
                "도구": "codex",
                "세션": f.stem,
                "브랜치": "",
                "PR링크": [],
                "초": _active_seconds(times),
                "출력토큰": out_tokens,
                "응답": replies,
                "처음": min(times),
                "마지막": max(times),
            })
    return blocks


def metric_agent_effort(prs: list[dict], repo: str,
                        claude_root: str | None, codex_root: str | None) -> dict:
    """에이전트 투입 — 지금까지 비어 있던 **분모**.

    나머지 지표가 "결과물이 얼마나 버티나"를 본다면 이건 "그 결과물에 얼마가
    들었나"를 본다. 둘을 나란히 놔야 비로소 "효율"이라는 말을 쓸 수 있다.

    **이것으로도 "AI 가 우리를 빠르게 하는가"에는 답 못 한다.** 대조군이 없어서다.
    답하는 것은 "어떤 PR 이 비쌌나"이고, 재작업한 PR 과 아닌 PR 의 투입을 비교할 수 있다.

    한계가 크다. 반드시 `주의` 필드와 같이 읽어라:
    - **기계 한 대의 로그다.** 다른 팀원 로그는 각자 자기 기계에서 돌려야 나온다.
    - 사람이 눈으로 검토한 시간, 손으로 테스트한 시간은 안 잡힌다.
    - Codex 는 브랜치를 안 남겨 PR 에 못 붙는다 (총량에만 들어간다).
    """
    base = {
        "설명": "에이전트 세션 로그에서 뽑은 투입 — 활성 시간과 출력 토큰",
        "주의": "기계 한 대 · 로컬 세션 로그 기준이다. 다른 팀원 로그도, 사람이 "
                "검토·테스트한 시간도 잡히지 않는다. 팀 전체 투입이 아니라 하한선이다.",
        "유휴_임계_분": int(SESSION_IDLE_GAP.total_seconds() // 60),
    }

    repo_name = repo.split("/")[-1]
    blocks: list[dict] = []
    sources: dict[str, str] = {}
    for label, raw, scan in (("claude-code", claude_root, scan_claude_sessions),
                             ("codex", codex_root, scan_codex_sessions)):
        if not raw:
            continue
        root = pathlib.Path(os.path.expanduser(raw))
        if not root.is_dir():
            sources[label] = f"{raw} 없음"
            continue
        found = scan(root, repo_name)
        blocks += found
        sources[label] = raw
    base["출처"] = sources

    if not blocks:
        return {**base, "활성_시간": None,
                "수집_상태": "세션 로그를 못 찾았다 — CI 에서는 정상이다(러너에 로그가 없다). "
                             "로컬에서 돌리면 채워진다."}

    tool_stats: dict[str, dict] = {}
    for b in blocks:
        t = tool_stats.setdefault(b["도구"], {"세션": set(), "초": 0.0, "출력토큰": 0})
        t["세션"].add(b["세션"])
        t["초"] += b["초"]
        t["출력토큰"] += b["출력토큰"]
    도구별 = {k: {"세션": len(v["세션"]),
                  "활성_시간": round(v["초"] / 3600, 1),
                  "출력_토큰": v["출력토큰"]}
              for k, v in sorted(tool_stats.items())}

    total_sec = sum(b["초"] for b in blocks)
    total_tok = sum(b["출력토큰"] for b in blocks)
    sessions = {b["세션"] for b in blocks}

    # ── PR 귀속 ─────────────────────────────────────────────
    # 브랜치명으로 붙이고, 브랜치가 안 맞는 세션만 pr-link 레코드로 보충한다.
    # 머지된 뒤 같은 브랜치명에서 난 작업은 그 PR 비용이 아니므로 뺀다
    # (머지 시 브랜치가 지워지지만, 이름을 재사용한 경우를 막는다).
    by_branch: dict[str, dict] = {}
    for p in prs:
        if not is_bot(p["author"]["login"]) and p.get("headRefName"):
            by_branch.setdefault(p["headRefName"], p)

    per_pr: dict[int, dict] = {}
    matched_sec = 0.0
    for b in blocks:
        pr = by_branch.get(b["브랜치"])
        if pr is None:
            # 브랜치로 못 붙은 블록은 pr-link 가 가리키는 PR 로 보낸다.
            cands = [n for n in b["PR링크"]
                     if any(q["number"] == n for q in prs)]
            if len(cands) != 1:
                continue
            pr = next(q for q in prs if q["number"] == cands[0])
        merged = parse_ts(pr.get("mergedAt"))
        if merged and b["처음"] > merged:
            continue
        e = per_pr.setdefault(pr["number"], {"pr": pr["number"], "제목": pr["title"],
                                             "초": 0.0, "출력토큰": 0, "응답": 0})
        e["초"] += b["초"]
        e["출력토큰"] += b["출력토큰"]
        e["응답"] += b["응답"]
        matched_sec += b["초"]

    human = [p for p in prs if not is_bot(p["author"]["login"])]
    hours = sorted(v["초"] / 3600 for v in per_pr.values())
    top = sorted(per_pr.values(), key=lambda v: -v["초"])[:10]

    귀속 = {
        "설명": "세션 로그를 PR 에 붙인 결과. 브랜치명이 1순위, pr-link 레코드가 2순위",
        "귀속된_PR수": len(per_pr),
        "사람_PR수": len(human),
        "커버리지": round(len(per_pr) / len(human) * 100, 1) if human else None,
        "귀속된_시간_비율": round(matched_sec / total_sec * 100, 1) if total_sec else None,
        "PR당_활성시간_중앙값": round(statistics.median(hours), 2) if hours else None,
        "PR당_활성시간_최대": round(max(hours), 2) if hours else None,
        "주의": "커버리지가 낮으면 아래 비교를 믿지 마라. 브랜치를 안 거친 작업, "
                "다른 기계에서 한 작업, 로그 보관 기간이 지난 작업은 안 붙는다.",
        "상위_PR": [{"pr": v["pr"], "제목": v["제목"],
                     "활성_시간": round(v["초"] / 3600, 2),
                     "출력_토큰": v["출력토큰"], "응답": v["응답"]} for v in top],
        # 전수 비교(재작업 대조)를 위해 PR 번호 → 활성 시간만 따로 싣는다.
        # 제목·토큰까지 다 실으면 스냅샷이 커지고, 비교에는 시간만 있으면 된다.
        "PR별_활성시간": {str(v["pr"]): round(v["초"] / 3600, 2)
                          for v in sorted(per_pr.values(), key=lambda x: x["pr"])},
    }

    return {
        **base,
        "기간": {"처음": min(b["처음"] for b in blocks).date().isoformat(),
                 "마지막": max(b["마지막"] for b in blocks).date().isoformat()},
        "합계": {"세션": len(sessions),
                 "활성_시간": round(total_sec / 3600, 1),
                 "출력_토큰": total_tok},
        "도구별": 도구별,
        "PR_귀속": 귀속,
    }


def metric_effort_vs_rework(effort: dict, rework: dict, prs: list[dict]) -> dict:
    """재작업한 PR 과 아닌 PR 의 투입 비교 — 이 저장소가 새로 답할 수 있게 된 질문.

    "재작업률 41%" 하나만으로는 해석이 갈린다. 30분짜리 PR 의 41% 와 8시간짜리
    PR 의 41% 는 전혀 다른 이야기다. 투입을 붙이면 그 구분이 생긴다.

    **인과가 아니다.** 큰 작업이 오래 걸리고 파일도 많이 건드리니 재작업으로
    잡힐 확률도 기계적으로 높다. 교란 요인을 통제하지 않았다.
    """
    base = {"설명": "재작업된 PR vs 아닌 PR 의 에이전트 투입",
            "주의": "인과가 아니다. 큰 작업일수록 오래 걸리고 재작업으로도 잡히기 쉽다. "
                    "교란을 통제하지 않은 단순 비교다."}

    hours = {int(k): v for k, v in
             (effort.get("PR_귀속", {}).get("PR별_활성시간") or {}).items()}
    if not hours:
        return {**base, "상태": "투입 데이터가 없어 비교할 수 없다"}

    reworked = set(rework.get("재작업_PR목록") or [])
    if not reworked:
        return {**base, "상태": "재작업 PR 목록이 없어 비교할 수 없다"}

    size = {p["number"]: p.get("additions", 0) + p.get("deletions", 0) for p in prs}
    merged_at = {p["number"]: parse_ts(p.get("mergedAt")) for p in prs}

    # ── 우측 절단(right censoring) 을 걷어낸다 ──────────────────────
    # 재작업은 "머지 후 7일 안에 다시 고쳐졌나"라서 **지연 지표**다. 어제 머지된
    # PR 은 아직 고쳐질 시간이 없었을 뿐인데 기계적으로 '재작업_없음' 으로 떨어진다.
    # 그대로 비교하면 최근 PR 이 통째로 한쪽 그룹을 부풀린다. 관측 창이 아직 안 닫힌
    # PR 은 아예 뺀다 — 표본이 줄더라도 없는 사실을 지어내는 것보다 낫다.
    latest = max((t for t in merged_at.values() if t), default=None)
    cutoff = latest - dt.timedelta(days=REWORK_WINDOW_DAYS) if latest else None
    censored = [n for n in hours
                if cutoff and merged_at.get(n) and merged_at[n] > cutoff]

    groups: dict[str, list[int]] = {"재작업됨": [], "재작업_없음": []}
    for n in hours:
        if n in censored:
            continue
        groups["재작업됨" if n in reworked else "재작업_없음"].append(n)

    def summarize(nums: list[int]) -> dict:
        if not nums:
            return {"표본": 0}
        hrs = sorted(hours[n] for n in nums)
        lines = sorted(size.get(n, 0) for n in nums)
        return {
            "표본": len(nums),
            "활성시간_중앙값": round(statistics.median(hrs), 2),
            "활성시간_합": round(sum(hrs), 1),
            "PR크기_중앙값": int(statistics.median(lines)),
            "PR": sorted(nums),
        }

    out = {k: summarize(v) for k, v in groups.items()}
    a, b = out["재작업됨"], out["재작업_없음"]
    small = min(a.get("표본", 0), b.get("표본", 0))

    if not small:
        out["판정"] = "비교 불가 — 한쪽 그룹이 비어 있다"
    elif small < MIN_COMPARE_N:
        # 여기서 배수를 내보내면 반드시 결론처럼 읽힌다. 그래서 안 내보낸다.
        out["판정"] = (f"표본 부족 — 작은 쪽이 {small}건(기준 {MIN_COMPARE_N}건)이라 "
                       f"중앙값이 한두 PR 에 좌우된다. 배수를 내지 않는다.")
        out["참고_배수"] = (round(a["활성시간_중앙값"] / b["활성시간_중앙값"], 2)
                            if b["활성시간_중앙값"] else None)
        out["참고_배수_경고"] = "표본이 기준 미만이라 참고값일 뿐이다. 인용하지 마라."
    else:
        out["배수"] = (round(a["활성시간_중앙값"] / b["활성시간_중앙값"], 2)
                       if b["활성시간_중앙값"] else None)
        out["판정"] = "읽을 수 있는 표본"

    out["읽는_법"] = (
        "배수가 1보다 크면 재작업한 PR 이 더 오래 걸린 것이다 — 투입을 늘려도 안 버텼다는 "
        "뜻이라 '더 오래 시키면 낫다'는 처방이 안 먹힌다. 1보다 작으면 급하게 만든 것이 "
        "다시 고쳐진 것이라 '덜 서두른다'가 처방이 된다. "
        "PR크기_중앙값 을 반드시 같이 봐라 — 두 그룹의 크기가 크게 다르면 이 비교는 "
        "'투입 차이'가 아니라 '작업 크기 차이'를 보고 있는 것이다."
    )
    return {**base,
            "표본_출처": "세션 로그가 PR 에 붙은 건 전수. 안 붙은 PR 은 빠져 있다",
            "관측창_미완료_제외": {
                "수": len(censored), "PR": sorted(censored),
                "이유": f"머지 후 {REWORK_WINDOW_DAYS}일이 안 지나 재작업 여부를 아직 모른다",
            },
            **out}


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
        ("에이전트 투입 (PR당 중앙값)",
         fmt(m.get('에이전트투입', {}).get('PR_귀속', {}).get('PR당_활성시간_중앙값'), "h"),
         "세션 로그 기준. **기계 한 대**의 값이라 팀 전체 투입이 아니다"),
        ("투입 귀속 커버리지",
         fmt(m.get('에이전트투입', {}).get('PR_귀속', {}).get('커버리지'), "%"),
         "사람 PR 중 세션 로그가 붙은 비율. 낮으면 위 줄을 믿지 마라"),
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
        "- **에이전트 투입은 분모다.** 재작업률 41% 는 30분짜리 PR 의 41% 와 8시간짜리",
        "  PR 의 41% 가 전혀 다른 이야기인데, 투입이 없으면 그 구분이 안 된다.",
        "  `투입_대_재작업` 절을 같이 봐라 — 단, 커버리지가 낮으면 아무 말도 못 한다.",
        "",
        "## 재현",
        "",
        "```bash",
        "python3 collect.py              # 리포트와 JSON 스냅샷 생성",
        "python3 collect.py --by-author  # 개인별 분해 (커밋하지 않는다)",
        "python3 collect.py --no-sessions  # 세션 로그를 읽지 않는다",
        "```",
        "",
        "**에이전트 투입은 각자 자기 기계에서 돌려야 채워진다.** 세션 로그가 로컬에만",
        "있기 때문이다. CI 실행은 이 칸을 비우고, 이전 스냅샷 값을 이어받는다.",
    ]
    return "\n".join(L) + "\n"


def self_test() -> int:
    """`--self-test` — 조용히 틀리는 두 가지를 막는다.

    둘 다 실제로 났던 버그다. 합계가 커지기만 할 뿐 예외가 안 나서
    **숫자를 따로 검산하지 않으면 못 잡는다.**
    """
    t0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    at = lambda m: t0 + dt.timedelta(minutes=m)
    fails = []

    def check(name: str, ok: bool, got=None):
        print(f"  {'✓' if ok else '✗'} {name}" + ("" if ok else f"  → {got}"))
        if not ok:
            fails.append(name)

    # ① 브랜치를 A→B→A 로 오가도 합계가 실제 경과시간을 넘으면 안 된다.
    #    (브랜치별로 따로 간격을 재면 B 에서 쓴 시간이 A 에도 더해진다.)
    got = _active_by_key([(at(0), "A"), (at(1), "A"), (at(2), "B"),
                          (at(3), "B"), (at(4), "A"), (at(5), "A")])
    check("교차 브랜치 합계 = 경과시간", abs(sum(got.values()) / 60 - 5.0) < 1e-9,
          {k: round(v / 60, 1) for k, v in got.items()})

    # ② 유휴 임계를 넘는 구간은 빠져야 한다.
    got = _active_by_key([(at(0), "A"), (at(1), "A"), (at(40), "A"), (at(41), "A")])
    check("유휴 39분 구간 제외", abs(sum(got.values()) / 60 - 2.0) < 1e-9,
          round(sum(got.values()) / 60, 1))

    # ③ 저장소 경로는 세그먼트로 맞춰야 한다 — 옆 저장소가 섞이면 안 된다.
    for path, want in (("/x/gole-project/GoLe/apps/api", True),
                       ("/x/gole-project/GoLe-obsidian", False),
                       ("/x/gole-project/GoLe-metrics", False),
                       ("/u/orca-workspaces/GoLe-quahog", True)):
        check(f"경로 매칭 {path} → {want}", _in_repo(path, "GoLe") is want)

    print("\n통과" if not fails else f"\n실패 {len(fails)}건: {fails}")
    return 1 if fails else 0


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
    ap.add_argument("--claude-sessions", default=CLAUDE_SESSION_ROOT,
                    help=f"Claude Code 세션 로그 경로 (기본 {CLAUDE_SESSION_ROOT})")
    ap.add_argument("--codex-sessions", default=CODEX_SESSION_ROOT,
                    help=f"Codex 세션 로그 경로 (기본 {CODEX_SESSION_ROOT})")
    ap.add_argument("--no-sessions", action="store_true",
                    help="세션 로그를 읽지 않는다 (에이전트 투입 지표를 비운다)")
    ap.add_argument("--self-test", action="store_true",
                    help="집계 로직만 자체 검증한다 (gh·네트워크 불필요)")
    args = ap.parse_args()

    if args.self_test:
        sys.exit(self_test())

    claude_root = None if args.no_sessions else args.claude_sessions
    codex_root = None if args.no_sessions else args.codex_sessions

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

    # 투입은 GitHub 이 아니라 로컬 세션 로그에서 온다. CI 러너에는 로그가 없어
    # 비어 나오는 것이 정상이고, 아래 이어받기 로직이 이전 값을 지켜 준다.
    print("  세션 로그 스캔 중...", file=sys.stderr)
    effort = metric_agent_effort(prs, args.repo, claude_root, codex_root)
    snap["지표"]["에이전트투입"] = effort
    snap["지표"]["투입_대_재작업"] = metric_effort_vs_rework(
        effort, snap["지표"]["재작업"], prs)
    if effort.get("합계"):
        print(f"  투입: 세션 {effort['합계']['세션']}개 · "
              f"활성 {effort['합계']['활성_시간']}시간 · "
              f"PR 귀속 {effort['PR_귀속']['귀속된_PR수']}건", file=sys.stderr)
    else:
        print("  투입: 세션 로그 없음 (CI 에서는 정상)", file=sys.stderr)

    root = pathlib.Path(args.out)
    (root / "data").mkdir(exist_ok=True)
    (root / "reports").mkdir(exist_ok=True)
    dpath = root / "data" / f"{today}.json"
    rpath = root / "reports" / f"{today}.md"

    # 같은 날 다시 돌릴 때, **데이터가 적은 실행이 많은 실행을 지우지 않게** 한다.
    # CI 는 볼트(비공개)를 못 읽어 '빠져나간 결함'이 비는데, 그대로 쓰면 로컬에서
    # 채워 둔 값이 사라진다. 비어 있는 칸만 이전 스냅샷에서 이어받는다.
    if dpath.exists():
        try:
            prev = json.loads(dpath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            prev = None
        if prev:
            old_esc = prev.get("지표", {}).get("빠져나간결함", {})
            new_esc = snap["지표"]["빠져나간결함"]
            if new_esc.get("이슈_수") is None and old_esc.get("이슈_수") is not None:
                snap["지표"]["빠져나간결함"] = {
                    **old_esc,
                    "출처": f"이번 실행은 볼트를 못 읽어 {prev.get('수집일')} 스냅샷 값을 이어받았다",
                }
                print("  빠져나간 결함: 이전 값을 이어받음 (볼트를 못 읽음)", file=sys.stderr)

            # 투입도 같은 이유로 지킨다. CI 러너에는 세션 로그가 없어서 매주
            # 월요일 자동 실행이 로컬에서 채운 값을 지워 버린다.
            old_eff = prev.get("지표", {}).get("에이전트투입", {})
            if not snap["지표"]["에이전트투입"].get("합계") and old_eff.get("합계"):
                snap["지표"]["에이전트투입"] = {
                    **old_eff,
                    "출처_메모": f"이번 실행은 세션 로그를 못 읽어 {prev.get('수집일')} 값을 이어받았다",
                }
                snap["지표"]["투입_대_재작업"] = prev.get("지표", {}).get(
                    "투입_대_재작업", snap["지표"]["투입_대_재작업"])
                print("  에이전트 투입: 이전 값을 이어받음 (세션 로그 없음)", file=sys.stderr)
    dpath.write_text(json.dumps(snap, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rpath.write_text(build_report(snap), encoding="utf-8")
    print(f"  → {dpath}\n  → {rpath}", file=sys.stderr)

    if args.by_author:
        print("\n개인별 (커밋하지 않는다):", file=sys.stderr)
        for k, v in sorted(metric_by_author(prs).items(), key=lambda x: -x[1]):
            print(f"  {k:20} {v}건", file=sys.stderr)


if __name__ == "__main__":
    main()
