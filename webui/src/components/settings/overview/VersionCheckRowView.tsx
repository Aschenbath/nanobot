import { useId, useRef } from "react";
import { ArrowUpCircle, Check, ExternalLink, Loader2, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { ToggleButton } from "@/components/ui/toggle-button";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import type { ReleaseStatus, UpdateChannel } from "@/lib/api";
import type { VersionResult } from "@/providers/VersionCheckProvider";

export type VersionInfoProps = {
  currentVersion?: string;
  currentCommit?: string | null;
  onRequestUpdate?: () => void;
  hasPendingUpdate?: boolean;
};
export type VersionCheckState = VersionResult;

export function VersionCheckRowView({
  currentVersion, currentCommit, checking, result, onCheck,
  onRequestUpdate, hasPendingUpdate, onDismiss, dismissedVersion,
  releaseStatus, canChangeChannel, changingChannel, channelError, channelSaved, onChangeChannel,
}: VersionInfoProps & {
  checking: boolean;
  result: VersionCheckState;
  onCheck: () => void;
  onDismiss?: () => void;
  dismissedVersion?: string | null;
  releaseStatus?: ReleaseStatus | null;
  canChangeChannel?: boolean;
  changingChannel?: boolean;
  channelError?: string;
  channelSaved?: boolean;
  onChangeChannel?: (channel: UpdateChannel) => Promise<void>;
}) {
  const { t } = useTranslation();
  const previewHelp = useId();
  const checkButton = useRef<HTMLButtonElement>(null);
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  return (
    <div className="flex w-full min-w-0 max-w-sm flex-col items-center gap-4">
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
      <div className="flex w-full min-w-0 flex-col items-center gap-2">
        <Button
          ref={checkButton}
          size="sm"
          variant="outline"
          onClick={onCheck}
          disabled={checking || changingChannel}
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
          ) : result?.type === "release-status" ? <span className="block max-w-sm text-center text-muted-foreground">{t(`updates.channel.${result.state}`)}</span>
            : result?.type === "error" ? <span className="text-destructive">{result.message}</span> : null}
        </div>
        {!checking && releaseStatus?.state === "withdrawn" && result?.type === "update" &&
          <p role="alert" className="max-w-sm text-[12px] text-destructive">{t("updates.channel.withdrawn")}</p>}
        {hasPendingUpdate && onRequestUpdate ? (
          <Button size="sm" variant="outline" onClick={onRequestUpdate}>
            {t("settings.about.viewUpdateDetails")}
          </Button>
        ) : result?.type === "update" && dismissedVersion !== result.latestVersion ? (
          <div className="mt-2 w-full min-w-0 rounded-panel bg-settings-surface p-4 text-start">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0" role="status">
                <div className="text-[13px] font-medium leading-5">
                  {t("settings.about.updateNotice")}
                </div>
                <p className="mt-1 break-all text-[12px] leading-5 tabular-nums text-muted-foreground">v{result.latestVersion}</p>
              </div>
              {onDismiss && <TooltipProvider><Tooltip>
                <TooltipTrigger asChild>
                  <Button size="icon" variant="ghost" onClick={() => {
                    onDismiss();
                    checkButton.current?.focus();
                  }}
                    className="-me-2 -mt-2 shrink-0 text-muted-foreground"
                    aria-label={t("settings.about.dismissUpdate")}>
                    <X className="h-4 w-4" aria-hidden />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>{t("settings.about.dismissUpdate")}</TooltipContent>
              </Tooltip></TooltipProvider>}
            </div>
            {!onRequestUpdate && <p className="mt-2 text-[12px] leading-5 text-muted-foreground">{t("settings.about.updateInstructions")}</p>}
            <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2">
              {onRequestUpdate ? <Button size="sm" onClick={onRequestUpdate}
                className="bg-blue-600 text-white hover:bg-blue-700 dark:bg-blue-500 dark:hover:bg-blue-600">{t("settings.about.updateNow")}</Button> :
                <Button size="sm" variant="outline" asChild><a href="https://github.com/HKUDS/nanobot#-install"
                  target="_blank" rel="noopener noreferrer">{t("settings.about.installationGuide")}<ExternalLink className="ms-1.5 h-3 w-3" aria-hidden /></a></Button>}
              {result.releaseUrl || result.pypiUrl ? (
                <a href={result.releaseUrl || result.pypiUrl} target="_blank" rel="noopener noreferrer"
                  className="inline-flex min-h-9 shrink-0 items-center gap-1 whitespace-nowrap rounded-control text-[12px] text-muted-foreground underline-offset-2 hover:text-foreground hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                  {result.releaseUrl ? t("updates.channel.releaseNotes") : "PyPI"}
                  <ExternalLink className="h-3 w-3 shrink-0" aria-hidden />
                </a>
              ) : null}
            </div>
          </div>
        ) : null}
      </div>
      {releaseStatus && onChangeChannel && <div className="w-full max-w-sm px-4 text-start text-[12px] text-muted-foreground">
        <div className="flex min-h-11 items-center justify-between gap-4">
          <span className="text-[13px] text-foreground">{t("updates.channel.receivePreview")}</span>
          <ToggleButton checked={releaseStatus.channel === "preview"}
            onChange={enabled => { void onChangeChannel(enabled ? "preview" : "stable"); }}
            disabled={!canChangeChannel || changingChannel || checking}
            label={t("updates.channel.receivePreview")} aria-describedby={previewHelp} />
        </div>
        <p id={previewHelp} className="pb-2 leading-5">
          {t("updates.channel.hint")}
          {!canChangeChannel && <span className="mt-2 block">{t("updates.blocked.permission")}</span>}
        </p>
        <p role="status" className="leading-5">
          {changingChannel ? t("updates.channel.saving") : channelError || (channelSaved ? t("updates.channel.saved") : "")}
        </p>
      </div>}
    </div>
  );
}
