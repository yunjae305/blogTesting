import { useState, type DragEvent } from "react";

/**
 * 파일을 끌어 놓거나 골라서 올리는 칸.
 *
 * 브랜드 자료 편집에서 시안대로 만든 것을, 새 글 작성의 '참고 자료'도 같은 것을 쓰도록
 * 여기로 옮겼다(2026-08-07 사용자 요청 — "브랜드 글 작성하기에 맞춰서 통일"). 두 화면이
 * 각자 파일 칸을 그리면 같은 일을 하는 자리가 서로 다르게 보인다.
 *
 * 화면이 "드래그하거나"라고 적으므로 **끌어 놓기를 실제로 받는다.** 고른 파일을 어떻게
 * 검사하고 저장할지는 이 칸이 정하지 않는다 — 그대로 `onFiles`에 넘긴다.
 */
export function FileDropZone({
  id,
  accept,
  multiple = true,
  disabled,
  onFiles,
  title,
  hint,
  ariaLabel,
}: {
  id: string;
  accept: string;
  multiple?: boolean;
  disabled?: boolean;
  onFiles: (files: File[]) => void;
  title: string;
  hint: string;
  ariaLabel: string;
}) {
  const [over, setOver] = useState(false);

  function drop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setOver(false);
    if (disabled) return;
    const files = [...(event.dataTransfer?.files ?? [])];
    if (files.length) onFiles(files);
  }

  return (
    <div
      className={`brand-dropzone${over ? " over" : ""}${disabled ? " disabled" : ""}`}
      onDragOver={(event) => {
        event.preventDefault();
        if (!disabled) setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={drop}
    >
      <span className="brand-dropzone-icon" aria-hidden="true">
        <svg
          viewBox="0 0 24 24"
          width="22"
          height="22"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M4 14.9A5 5 0 0 1 6.7 5.6a6.5 6.5 0 0 1 12.2 1.9A4.3 4.3 0 0 1 18 16" />
          <path d="M12 12v9M8.5 15.5 12 12l3.5 3.5" />
        </svg>
      </span>
      <span className="brand-dropzone-copy">
        <strong>{title}</strong>
        <span>{hint}</span>
      </span>
      <label className="button small brand-dropzone-pick" htmlFor={id}>
        파일 선택
      </label>
      {/* 입력 자체는 보이지 않게 두되 지우지는 않는다 — 키보드·보조기술은 이것으로 고른다. */}
      <input
        id={id}
        className="brand-dropzone-input"
        type="file"
        accept={accept}
        multiple={multiple}
        aria-label={ariaLabel}
        disabled={disabled}
        onChange={(event) => {
          const files = [...(event.target.files ?? [])];
          event.target.value = "";
          if (files.length) onFiles(files);
        }}
      />
    </div>
  );
}
