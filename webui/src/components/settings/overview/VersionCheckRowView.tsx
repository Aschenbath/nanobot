import { ArrowUpCircle, Check, ExternalLink, Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";

export type VersionInfoProps = {
  currentVersion?: string;
  currentCommit?: string | null;
  onRequestUpdate?: () => void;
  hasPendingUpdate?: boolean;
};
export type VersionCheckState =
  | { type: "up-to-date" }
  | { type: "update"; latestVersion: string; pypiUrl?: string }
  | { type: "error"; message: string }
  | null;

export function VersionCheckRowView({
  currentVersion, currentCommit, checking, result, onCheck,
  onRequestUpdate, hasPendingUpdate, onDismiss, dismissedVersion,
}: VersionInfoProps & {
  checking: boolean;
  result: VersionCheckState;
  onCheck: () => void;
  onDismiss?: () => void;
  dismissedVersion?: string | null;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  return (
    <div className="flex flex-col items-center gap-4">
      <div className="min-w-0">
        <div className="sr-only">
          {tx("settings.about.version", "Version")}
        </div>
        <div className="mt-0.5 text-[12px] leading-5 text-muted-foreground">
          {currentVersion ? `v${currentVersion}` : "nanobot"}
          {currentCommit && (
            <>
              <span aria-hidden> (</span>
              <a href={`https://github.com/HKUDS/nanobot/commit/${currentCommit}`}
                target="_blank" rel="noopener noreferrer" title={currentCommit}
                className="font-mono hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                {currentCommit.slice(0, 7)}
              </a>
              <span aria-hidden>)</span>
            </>
          )}
        </div>
      </div>
      <div className="flex shrink-0 flex-col items-center gap-2">
        <Button
          size="sm"
          variant="outline"
          onClick={onCheck}
          disabled={checking}
          aria-busy={checking}
          className="rounded-full disabled:opacity-100"
        >
          {checking ? (
            <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin motion-reduce:animate-none" aria-hidden />
          ) : (
            <ArrowUpCircle className="mr-1.5 h-3.5 w-3.5" aria-hidden />
          )}
          {checking
            ? tx("settings.about.checking", "Checking...")
            : tx("settings.about.checkForUpdates", "Check for updates")}
        </Button>
        <div role="status" aria-live="polite" aria-atomic="true" className="min-h-4 text-[12px]">
          {checking ? <span className="sr-only">{tx("settings.about.checking", "Checking...")}</span> : result?.type === "up-to-date" ? (
            <span className="inline-flex items-center gap-1.5 text-emerald-600 dark:text-emerald-300">
              <Check className="h-3 w-3" aria-hidden />
              {tx("settings.about.upToDate", "You're up to date")}
            </span>
          ) : result?.type === "error" ? <span className="text-destructive">{result.message}</span> : null}
        </div>
        {hasPendingUpdate && onRequestUpdate ? (
          <Button size="sm" variant="outline" onClick={onRequestUpdate}>
            {t("settings.about.viewUpdateDetails")}
          </Button>
        ) : result?.type === "update" ? (
          <div className="mt-2 max-w-sm rounded-panel bg-settings-surface p-4 text-start">
          <div className="flex items-center gap-1.5 text-[13px] font-medium text-blue-600 dark:text-blue-300" role="status">
            <ArrowUpCircle className="h-3 w-3" aria-hidden />
            {t("settings.about.updateAvailable", {
              defaultValue: "Update available v{{version}}",
              version: result.latestVersion,
            })}
            {result.pypiUrl ? (
              <a
                href={result.pypiUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-0.5 underline-offset-2 hover:underline"
              >
                PyPI
                <ExternalLink className="h-2.5 w-2.5" aria-hidden />
              </a>
            ) : null}
          </div>
          {!onRequestUpdate && <p className="mt-2 text-[12px] leading-5 text-muted-foreground">{t("settings.about.updateInstructions")}</p>}
          <div className="mt-3 flex flex-wrap items-center gap-2">
            {onRequestUpdate ? <Button size="sm" onClick={onRequestUpdate}
              className="bg-blue-600 text-white hover:bg-blue-700 dark:bg-blue-500 dark:hover:bg-blue-600">{t("settings.about.updateNow")}</Button> :
              <Button size="sm" variant="outline" asChild><a href="https://github.com/HKUDS/nanobot#-install"
                target="_blank" rel="noopener noreferrer">{t("settings.about.installationGuide")}<ExternalLink className="ms-1.5 h-3 w-3" aria-hidden /></a></Button>}
            {onDismiss && dismissedVersion !== result.latestVersion && <Button size="sm" variant="ghost" onClick={onDismiss}>{t("settings.about.dismissUpdate")}</Button>}
          </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}
