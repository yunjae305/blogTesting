import type { ScheduledJob } from "../../api/types";
import { byQueueOrder } from "./jobOrder";

/**
 * 같은 소재로 여러 편을 걸었을 때 **몇 편째인지**(2026-08-12 사용자 요청).
 *
 *     "이렇게 소재가 같은 경우에는 첫번째 원고인지 두번째 원고인지 사용자가
 *      파악할 수 있게도 해야지."
 *
 * 소재 하나로 2·3편을 만들면 작업 큐에 같은 이름의 줄이 나란히 서고, 작업 현황에는
 * **글자 하나 다르지 않은 줄**이 두 번씩 찍힌다("'롯데리아'의 원고와 이미지를 생성하고
 * 있습니다"가 두 줄). 어느 편의 이야기인지 알 수 없을 뿐 아니라 화면이 같은 줄을 두 번
 * 그린 것처럼 보인다.
 *
 * 한 편뿐인 소재에는 **붙이지 않는다.** 모든 줄에 '1편째'가 붙으면 구분이 되지 않고,
 * 소재별 한 편씩 거는 자동 포스팅 화면이 통째로 시끄러워진다.
 *
 * 세는 단위는 **한 번에 건 묶음**이다(2026-08-13 사용자 지적: "한번에 예약할때 잡히는
 * 것들을 한덩어리로 보고 거기서 1편2편3편으로 표기해야지"). 소재로만 묶으면 새 글
 * 작성에서 같은 소재를 여러 번 건 것이 전부 한 줄에 서서 '6편째'가 나온다 — 그 경로는
 * **돌고 있는 배치에 계속 붙기** 때문에 배치로 묶어도 마찬가지다. 그래서 서버가 등록마다
 * 새로 발급하는 `seriesId`를 본다. 옛 작업에는 그 값이 없으므로 예전처럼 소재로 묶는다.
 *
 * 번호는 **작업 큐에 보이는 순서**로 매긴다(byQueueOrder) — 화면에서 위에 있는 줄이
 * 1편째다. 배치 안의 sequence를 그대로 쓰지 않는 이유는, 여러 배치의 작업이 한 목록에
 * 섞여 올 수 있어 sequence만으로는 같은 묶음끼리의 순서가 되지 않기 때문이다.
 */
export function draftLabels(jobs: ScheduledJob[]): Record<string, string> {
  const byGroup = new Map<string, ScheduledJob[]>();
  for (const job of jobs) {
    // 옛 작업(seriesId 없음)끼리는 예전 그대로 소재로 묶는다. 접두사를 붙여 두어야
    // 소재 이름이 우연히 다른 묶음의 id와 같아지는 일이 없다.
    const key = job.seriesId ?? `topic:${job.topic ?? ""}`;
    const group = byGroup.get(key);
    if (group) group.push(job);
    else byGroup.set(key, [job]);
  }

  const labels: Record<string, string> = {};
  for (const group of byGroup.values()) {
    if (group.length < 2) continue;
    [...group].sort(byQueueOrder).forEach((job, index) => {
      labels[job.jobId] = `${index + 1}편째`;
    });
  }
  return labels;
}

/** 로그 한 줄 앞에 붙일 표시. 붙일 것이 없으면 문장을 그대로 둔다. */
export function withDraftLabel(message: string, label: string | undefined): string {
  return label ? `${label} · ${message}` : message;
}
