import type { ScheduledJob } from "../../api/types";

/**
 * 작업 목록의 **표시 순서**: 최근 예약이 위, 배치 안에서는 올라갈 순서대로.
 *
 * 서버의 `repository.list_user_jobs`와 **같은 규칙이어야 한다.** 그런데 화면은 두 조회를
 * 합쳐 보므로(내 예약 전부 + 지금 도는 배치) 이어 붙이는 순간 순서가 다시 흐트러진다 —
 * 목록에 아직 안 담긴 작업이 뒤에 붙기 때문이다. 합친 뒤 한 번 더 세워 준다.
 *
 * 키가 셋이고 각각 이유가 있다: 최근 배치 먼저(`createdAt`), 절대 시각 예약은 정해 둔
 * 시각 순(`publishAt`), 나머지는 입력한 순서(`sequence`).
 *
 * 마지막 키가 이번에 더해졌다. 간격 방식 배치는 앞의 두 키가 통째로 동점이라 — 한 배치의
 * 작업은 `createdAt`이 같고 `publishAt`은 아예 없다 — 순서가 정해지지 않았고 실제로
 * 뒤집혀 나왔다: GS25 · 세븐일레븐 순으로 넣었는데 세븐일레븐이 위에 섰다(2026-08-06).
 */
export function byQueueOrder(a: ScheduledJob, b: ScheduledJob): number {
  // 최근 예약이 먼저. 같은 배치면 createdAt이 같으므로 아래 두 키가 가른다.
  if (a.createdAt !== b.createdAt) return a.createdAt < b.createdAt ? 1 : -1;
  // 시각이 없는 작업(간격 방식)은 앞에 둔다 — 서버(Mongo)의 null 처리와 같은 자리다.
  const left = a.publishAt ?? "";
  const right = b.publishAt ?? "";
  if (left !== right) return left < right ? -1 : 1;
  return a.sequence - b.sequence;
}

/** 끝난 작업의 순서: **최근에 끝난 것부터.** 발행 내역은 일지처럼 읽혀야 한다. */
export function byFinishedOrder(a: ScheduledJob, b: ScheduledJob): number {
  const left = finishedAt(a);
  const right = finishedAt(b);
  if (left !== right) return left < right ? 1 : -1;
  // 같은 시각이면 큐와 같은 규칙으로 가른다(같은 배치 안의 순서를 뒤집지 않는다).
  return byQueueOrder(a, b);
}

/**
 * 이 작업이 **끝난 시각**. 발행에 성공했으면 발행 시각, 실패했으면 마지막 시도 시각이다.
 *
 * 어느 것도 없으면(취소·원고를 만들기 전에 끝난 작업) 마지막으로 바뀐 시각을 쓴다 —
 * 정렬에서 빠지지 않게 하려는 것이고, 화면에는 그때 시각 대신 '—'를 적는다.
 */
export function finishedAt(job: ScheduledJob): string {
  return job.publishedAt || job.lastAttemptAt || job.updatedAt || job.createdAt || "";
}
