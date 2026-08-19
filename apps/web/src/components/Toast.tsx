import { useStore } from "../store";

export function Toast() {
  const { toast } = useStore();

  return (
    <div
      id="toast"
      className={`toast ${toast ? "show" : ""} ${toast?.isError ? "error" : ""}`}
      role="status"
      aria-live="polite"
    >
      {toast?.message ?? ""}
    </div>
  );
}
