import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Loader2 } from "lucide-react";

import { AlertDialog, AlertDialogContent, AlertDialogTitle, AlertDialogDescription } from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { ApiError, createUpdateReview, readUpdateReview, readUpdateJob, prepareUpdate, executeUpdate,
  type UpdateReviewResponse, type UpdateJob } from "@/lib/api";
import type { NanobotClient } from "@/lib/nanobot-client";

const STORAGE_KEY = "nanobot.pending-update.v1";
const terminal = new Set(["preparation_failed", "handoff_failed", "restart_failed", "succeeded", "failed", "cancelled"]);
const UpdateFlowContext = createContext<{ open: (() => void) | undefined }>({ open: undefined });

function savedJob(): string | null {
  try {
    const id = sessionStorage.getItem(STORAGE_KEY);
    return id && /^[0-9a-f-]{36}$/.test(id) ? id : null;
  } catch { return null; }
}
function remember(id: string | null) {
  try { if (id) sessionStorage.setItem(STORAGE_KEY, id); else sessionStorage.removeItem(STORAGE_KEY); } catch { /* Memory state still works. */ }
}

export function UpdateFlowProvider({ client, token, enabled, children }: {
  client: NanobotClient; token: string; enabled: boolean; children: ReactNode;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [review, setReview] = useState<UpdateReviewResponse | null>(null);
  const [job, setJob] = useState<UpdateJob | null>(null);
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [offline, setOffline] = useState(false);
  const [delayed, setDelayed] = useState(false);
  const [pollAttempt, setPollAttempt] = useState(0);
  const generation = useRef(0);
  const approved = useRef<string | null>(null);
  const trigger = useRef<HTMLElement | null>(null);
  const inFlight = useRef(false);

  // Credentials/host changes revoke this client's unconsumed consent. Restoring
  // a durable job is a read only; READY after refresh needs another button press.
  useEffect(() => {
    generation.current++;
    approved.current = null;
    inFlight.current = false;
    setSubmitting(false);
    setReview(null);
    setJob(null);
    setError("");
    const id = enabled ? savedJob() : null;
    setPendingId(id);
    setOpen(!!id);
  }, [client, token, enabled]);

  const execute = useCallback(async (id: string, previous?: string) => {
    if (inFlight.current) return;
    inFlight.current = true;
    setSubmitting(true);
    const current = generation.current;
    approved.current = null; // Consume once before the request, including a lost response.
    try {
      const response = await executeUpdate(client, id, previous);
      if (generation.current === current) setJob(response.job);
    } catch {
      // The gateway may already be shutting down. Read the journal before
      // offering a fresh confirmation; never replay execution automatically.
      if (generation.current === current) setError(t("updates.confirmationUnknown"));
    } finally {
      if (generation.current === current) { inFlight.current = false; setSubmitting(false); }
    }
  }, [client, t]);

  useEffect(() => {
    if (!enabled || !pendingId) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    let missingSince = 0;
    const refresh = async () => {
      try {
        const { job: next } = await readUpdateJob(token, pendingId);
        if (!active) return;
        setJob(next);
        setOffline(false);
        setDelayed(false);
        missingSince = 0;
        setError("");
        if (next.recorded_phase === "ready" && approved.current === next.id) {
          await execute(next.id);
        }
        if (!active) return;
        if (terminal.has(next.recorded_phase) && !(next.recorded_phase === "succeeded" && next.running_version !== next.target_version)) return;
        // A refresh can recover files already staged, but not installation consent.
        if (next.recorded_phase === "ready" && !next.execution_confirmation_id) {
          try {
            const restored = await readUpdateReview(token, next.id);
            if (active) setReview(restored);
          } catch { if (active) setError(t("updates.reviewFailed")); }
        }
      } catch (reason) {
        if (!active) return;
        if (reason instanceof ApiError && reason.status === 404) {
          // Do not discard an unknown mutation outcome or create a second job.
          approved.current = null;
          setError(t("updates.statusFailed"));
          return;
        }
        if (reason instanceof ApiError && [401, 403, 409].includes(reason.status)) {
          approved.current = null;
          setError(t("updates.statusFailed"));
          return;
        }
        missingSince ||= Date.now();
        setOffline(true);
        setDelayed(Date.now() - missingSince > 120_000);
      }
      if (active) timer = setTimeout(() => void refresh(), 1500);
    };
    void refresh();
    return () => { active = false; clearTimeout(timer); };
  }, [enabled, pendingId, token, execute, t, pollAttempt]);

  const inspect = async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    const current = ++generation.current;
    approved.current = null;
    setSubmitting(true);
    setError("");
    setJob(null);
    setReview(null);
    setPendingId(null);
    remember(null);
    try {
      const next = await createUpdateReview(client);
      if (generation.current === current) setReview(next);
    } catch {
      if (generation.current === current) setError(t("updates.reviewFailed"));
    } finally {
      if (generation.current === current) { setSubmitting(false); inFlight.current = false; }
    }
  };
  const show = () => {
    trigger.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setOpen(true);
    if (!pendingId) void inspect();
  };
  const confirm = async () => {
    if (inFlight.current || !review?.execution_available || !review.installation_allowed) return;
    const id = review.plan.id;
    if (job?.recorded_phase === "ready") {
      if (job.execution_confirmation_id) return;
      await execute(id);
      setPollAttempt(value => value + 1);
      return;
    }
    inFlight.current = true;
    const current = generation.current;
    approved.current = id;
    setSubmitting(true);
    setError("");
    remember(id);
    try {
      const response = await prepareUpdate(client, id);
      if (generation.current === current) { setJob(response.job); setPendingId(id); }
    } catch {
      if (generation.current === current) {
        // Recover the request outcome without sending a second mutation.
        approved.current = null;
        setPendingId(id);
      }
    } finally {
      if (generation.current === current) { setSubmitting(false); inFlight.current = false; }
    }
  };

  const phase = job?.recorded_phase;
  const done = job?.recorded_phase === "succeeded" && job.running_version === job.target_version;
  const awaitingRestart = phase === "succeeded" && !done;
  const failed = !!phase && terminal.has(phase) && phase !== "succeeded";
  const reviewing = submitting && !review && !pendingId;
  const busy = submitting || (!!pendingId && !error && !failed && !done && (phase !== "ready" || !!job?.execution_confirmation_id));
  const title = done ? "done" : offline || awaitingRestart ? "reconnecting" : reviewing ? "reviewingTitle" : busy ? phase === "preparation_requested" || phase === "preparing" ? "preparing" : phase === "installing" ? "installing" : "working" : failed ? "failed" : "confirm";
  const blocked = !job?.execution_confirmation_id && review && (!review.installation_allowed ? "permission" : review.plan.execution_blocked_reason);
  const description = delayed ? t("updates.delayed") : error || (done ? t("updates.doneDescription", { version: job?.target_version })
    : offline || awaitingRestart ? t("updates.reconnectingDescription") : failed ? t(job?.mutation_started ? "updates.repairRequired" : "updates.preparationFailed")
    : reviewing ? t("updates.reviewing") : busy ? t("updates.workingDescription") : blocked ? t(`updates.blocked.${blocked}`)
    : review ? t("updates.confirmDescription", { version: review.plan.target_version, host: review.plan.host }) : t("updates.reviewing"));
  const retryHandoff = phase === "handoff_failed" && !job?.mutation_started && !!job?.execution_confirmation_id;
  const startAgain = !pendingId || (failed && !job?.mutation_started && !job?.execution_confirmation_id);
  const primary = () => {
    if (done) close();
    else if (retryHandoff && job?.execution_confirmation_id) {
      setError("");
      void execute(job.id, job.execution_confirmation_id).then(() => setPollAttempt(value => value + 1));
    } else if (error || failed) {
      if (startAgain) void inspect();
      else { setError(""); setPollAttempt(value => value + 1); }
    } else void confirm();
  };
  const close = () => { setOpen(false); if (done) { remember(null); setPendingId(null); window.location.reload(); } };

  return <UpdateFlowContext.Provider value={{ open: enabled ? show : undefined }}>
    {children}
    <AlertDialog open={open} onOpenChange={setOpen}>
      <AlertDialogContent className="max-w-[384px] gap-0 rounded-panel p-6 motion-reduce:animate-none"
        onCloseAutoFocus={(event) => { event.preventDefault(); if (trigger.current?.isConnected) trigger.current.focus(); }}>
        <div aria-live="polite" aria-atomic="true">
          <img src="/brand/nanobot_mark.svg" alt="" draggable={false} className="mb-4 h-11 w-11 select-none" />
          <AlertDialogTitle className="text-lg font-semibold leading-7 tracking-tight">{t(`updates.${title}`)}</AlertDialogTitle>
          <AlertDialogDescription className="mt-2 text-sm leading-6">{description}</AlertDialogDescription>
          {review && !busy && !blocked && !done && review.plan.profiles.filter(p => p.restart).length > 1 &&
            <p className="mt-2 text-xs text-muted-foreground">{t("updates.sharedInstallation", { count: review.plan.profiles.filter(p => p.restart).length })}</p>}
        </div>
        <div className={`mt-6 grid ${done ? "grid-cols-1" : "grid-cols-2"} gap-2.5`}>
          {!done && <Button variant="secondary" className="h-10 dark:bg-foreground/10 dark:hover:bg-foreground/15" onClick={close}>
            {t(done || failed || error || blocked ? "updates.close" : busy ? "updates.hide" : "updates.cancel")}
          </Button>}
          <Button className="h-10 bg-blue-600 text-white hover:bg-blue-700" disabled={busy || !!blocked || (failed && !!job?.mutation_started)}
            onClick={primary}>
            {busy && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin motion-reduce:animate-none" aria-hidden />}
            {t(done ? "updates.return" : busy ? "updates.wait" : error || failed ? "updates.checkAgain" : "updates.apply")}
          </Button>
        </div>
      </AlertDialogContent>
    </AlertDialog>
  </UpdateFlowContext.Provider>;
}

export function useUpdateFlow() { return useContext(UpdateFlowContext); }
