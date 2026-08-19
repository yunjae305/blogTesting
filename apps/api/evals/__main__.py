"""기준선 측정 CLI.

    cd apps/api
    python -m evals                                  # fixture(무료). 기준선 아님
    python -m evals --mode live-titles               # M2 제목만 실제 호출
    python -m evals --mode live-full --limit 3       # M4 7단계까지 실제 호출(비쌈)
    python -m evals --mode live-titles --tags conflict

결과는 `--out` 경로에 JSON으로 저장하고, 요약표를 표준출력에 찍는다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
from pathlib import Path

from . import runner


def _mean(values: list[float]) -> float:
    return round(statistics.fmean(values), 3) if values else 0.0


def _summarise(report: dict) -> str:
    lines: list[str] = []
    cases = report["cases"]
    lines.append(f"모드={report['mode']} 모델={report['model']} 사례={report['case_count']}개")

    failed = [case for case in cases if case.get("error")]
    if failed:
        lines.append(f"실패 {len(failed)}건: " + ", ".join(case["case_id"] for case in failed))

    titles = [case["titles"] for case in cases if case.get("titles")]
    if titles:
        lines.append("")
        lines.append("[제목]")
        lines.append(f"  길이 준수율 평균        {_mean([t['length_compliance'] for t in titles])}")
        lines.append(f"  후보 간 어휘 중복 평균  {_mean([t['mean_pairwise_noun_overlap'] for t in titles])}")
        lines.append(f"  같은 시작 표현 합계     {sum(t['duplicate_start_patterns'] for t in titles)}")
        lines.append(f"  같은 관점 중복 합계     {sum(t['same_angle_pairs'] for t in titles)}")
        lines.append(f"  낚시 표현 합계          {sum(t['clickbait_titles'] for t in titles)}")
        lines.append(f"  출처 없는 수치 합계     {sum(t['titles_with_unsourced_numbers'] for t in titles)}")
        hooks: dict[str, int] = {}
        for item in titles:
            for hook, count in item["hook_type_distribution"].items():
                hooks[hook] = hooks.get(hook, 0) + count
        lines.append("  hookType 분포          " + ", ".join(f"{k}={v}" for k, v in sorted(hooks.items())))

    drafts = [case["draft"] for case in cases if case.get("draft")]
    if drafts:
        lines.append("")
        lines.append("[원고]")
        lines.append(f"  분량 준수율             {_mean([1.0 if d['within_target'] else 0.0 for d in drafts])}")
        lines.append(f"  본문 글자수 평균        {_mean([d['body_chars'] for d in drafts])}")
        lines.append(f"  3-gram 반복률 평균      {_mean([d['repeated_ngram_rate'] for d in drafts])}")
        lines.append(f"  도입부 상투구 있는 글    {sum(1 for d in drafts if d['intro_cliche_hits'])}/{len(drafts)}")
        lines.append(f"  연결어 최다 반복 평균    {_mean([d['max_connective_repeats'] for d in drafts])}")
        lines.append(f"  문단 길이 균일 글        {sum(1 for d in drafts if d['uniform_paragraph_length'])}/{len(drafts)}")
        lines.append(f"  소제목 문법 균일 글      {sum(1 for d in drafts if d['uniform_h2_grammar'])}/{len(drafts)}")
        lines.append(f"  결론-본문 중복 평균      {_mean([d['conclusion_body_overlap'] for d in drafts])}")
        lines.append(f"  근거 없는 1인칭 있는 글  {sum(1 for d in drafts if d['experience_claim_hits'])}/{len(drafts)}")
        lines.append(f"  근거 없는 수치 있는 글   {sum(1 for d in drafts if d['unsupported_numeric_claims'])}/{len(drafts)}")
        lines.append(f"  품질검사 통과            {sum(1 for d in drafts if d['quality_ok'])}/{len(drafts)}")

    regen = [case["regenerated"] for case in cases if case.get("regenerated")]
    if regen:
        lines.append("")
        lines.append("[제목 재생성 — 두 번째 배치]")
        lines.append(f"  이전 (hookType,titleType) 조합 재사용 합계 {sum(r['repeated_hook_title_combos'] for r in regen)}")
        lines.append(f"  이전 제목과의 최대 유사도 평균            {_mean([r['max_similarity_to_excluded'] for r in regen])}")
        lines.append(f"  길이 준수율 평균                          {_mean([r['length_compliance'] for r in regen])}")
        lines.append(f"  같은 시작 표현 합계                        {sum(r['duplicate_start_patterns'] for r in regen)}")

    calls = [call for case in cases for call in case.get("calls", [])]
    if calls:
        lines.append("")
        lines.append("[provider 호출]")
        lines.append(f"  호출 수                 {len(calls)}")
        truncated = [call for call in calls if call.get("truncated")]
        lines.append(f"  max_tokens로 잘린 호출   {len(truncated)}")
        if truncated:
            for call in truncated:
                lines.append(f"    - {call['stage']} (max_tokens={call['max_tokens']})")
        reasons: dict[str, int] = {}
        for call in calls:
            reason = str(call.get("stop_reason"))
            reasons[reason] = reasons.get(reason, 0) + 1
        lines.append("  stop_reason 분포        " + ", ".join(f"{k}={v}" for k, v in sorted(reasons.items())))
        lines.append(f"  입력 토큰 합계          {sum(call['input_tokens'] for call in calls)}")
        lines.append(f"  출력 토큰 합계          {sum(call['output_tokens'] for call in calls)}")
        by_stage: dict[str, list[int]] = {}
        for call in calls:
            by_stage.setdefault(call["stage"], []).append(call["output_tokens"])
        lines.append("  단계별 출력 토큰 최대")
        for stage, values in sorted(by_stage.items()):
            lines.append(f"    {stage:<16} 최대 {max(values):>6}  평균 {int(_mean(values)):>6}")

    if report.get("unknown_fixture_tools"):
        lines.append("")
        lines.append("fixture 응답이 없는 도구: " + ", ".join(report["unknown_fixture_tools"]))

    stages = {}
    for case in cases:
        for stage, ok in (case.get("stages_parsed") or {}).items():
            hit, total = stages.get(stage, (0, 0))
            stages[stage] = (hit + (1 if ok else 0), total + 1)
    if stages:
        lines.append("")
        lines.append("[단계별 파싱 성공]")
        for stage, (hit, total) in sorted(stages.items()):
            lines.append(f"  {stage:<16} {hit}/{total}")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals")
    parser.add_argument(
        "--mode",
        default="fixture",
        choices=["fixture", "live-titles", "live-regen", "live-full"],
        help="fixture는 실제 API를 부르지 않는다(기본). live-*는 실제 비용이 발생한다.",
    )
    parser.add_argument("--limit", type=int, default=None, help="앞에서 N개 사례만")
    parser.add_argument("--tags", default=None, help="쉼표로 구분한 태그 필터(conflict, control 등)")
    parser.add_argument("--cases", default=None, help="쉼표로 구분한 case_id 부분 문자열 필터")
    parser.add_argument(
        "--model",
        default=None,
        help="Anthropic 모델 ID. 기본값은 서버와 같은 곳(.env의 M2_TOPIC_MODEL)을 읽는다.",
    )
    parser.add_argument("--out", default=None, help="JSON 저장 경로")
    args = parser.parse_args(argv)

    # 서버와 같은 방식으로 .env를 읽는다 — 기준선은 실제로 배포된 설정으로 재야 의미가 있다.
    if not os.environ.get("ANTHROPIC_API_KEY"):
        from app.config import load_env_file

        load_env_file()

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if args.mode != "fixture" and not api_key:
        print("ANTHROPIC_API_KEY가 없어 실제 호출을 할 수 없습니다.", file=sys.stderr)
        return 2

    out = Path(args.out) if args.out else Path("evals") / f"baseline-{args.mode}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    # 진행 중 결과를 사례마다 append한다. 끊겨도 여기까지는 남는다.
    progress = out.with_suffix(".jsonl")
    if progress.exists():
        progress.unlink()

    report = asyncio.run(
        runner.run(
            mode=args.mode,
            limit=args.limit,
            model=args.model,
            api_key=api_key or "eval-fixture-key",
            only_tags=set(args.tags.split(",")) if args.tags else None,
            only_ids=args.cases.split(",") if args.cases else None,
            progress_path=progress,
        )
    )

    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(_summarise(report))
    print(f"\nJSON: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
