import { type RefObject, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { MarkdownText } from "@/components/MarkdownText";
import { floatingSurfaceElevationClassName } from "@/components/ui/floating-surface";
import { cn } from "@/lib/utils";
import type { UIMessage } from "@/lib/types";
import { type PromptAnchor, promptTop, userPromptAnchors } from "./promptNavigation";

interface PromptRailProps {
  bottomOffset: number;
  messages: UIMessage[];
  prompts?: PromptAnchor[];
  onJumpToPrompt: (promptId: string) => void;
  scrollRef: RefObject<HTMLDivElement>;
}

const GAP = 16;
const EDGE = 20;
const WIDTHS = [28, 22, 16, 11];

/** Complete logical list; only the visible markers and one preview are mounted. */
export function PromptRail({ bottomOffset, messages, prompts, onJumpToPrompt, scrollRef }: PromptRailProps) {
  const { t } = useTranslation();
  const anchors = useMemo(() => prompts ?? userPromptAnchors(messages), [prompts, messages]);
  const railRef = useRef<HTMLDivElement>(null);
  const inside = useRef(false);
  const [height, setHeight] = useState(300);
  const [scrollTop, setScrollTop] = useState(0);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [focused, setFocused] = useState<number | null>(null);
  const contentHeight = Math.max(height, (anchors.length - 1) * GAP + EDGE * 2);
  const padding = (contentHeight - (anchors.length - 1) * GAP) / 2;
  const first = Math.max(0, Math.floor((scrollTop - padding) / GAP) - 4);
  const end = Math.min(anchors.length, Math.ceil((scrollTop + height - padding) / GAP) + 5);

  useEffect(() => {
    const rail = railRef.current;
    if (!rail) return;
    const measure = () => setHeight(rail.clientHeight || 300);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(rail);
    return () => observer.disconnect();
  }, [anchors.length >= 2, bottomOffset]);

  useEffect(() => {
    const scroller = scrollRef.current;
    if (!scroller) return;
    // Measure only mounted messages, not the complete navigation index.
    let positions: { id: string; top: number }[] = [];
    let frame = 0;
    const update = () => {
      const cursor = scroller.scrollTop + 96;
      let low = 0;
      let high = positions.length - 1;
      while (low <= high) {
        const middle = (low + high) >>> 1;
        if (positions[middle].top <= cursor) low = middle + 1;
        else high = middle - 1;
      }
      setActiveId(positions[Math.max(0, high)]?.id ?? null);
    };
    const measure = () => {
      positions = Array.from(scroller.querySelectorAll<HTMLElement>("[data-user-prompt-id]"),
        (element) => ({ id: element.dataset.userPromptId!, top: promptTop(scroller, element) - 16 }));
      update();
    };
    const schedule = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(update);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(scroller);
    if (scroller.firstElementChild) observer.observe(scroller.firstElementChild);
    scroller.addEventListener("scroll", schedule, { passive: true });
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
      scroller.removeEventListener("scroll", schedule);
    };
  }, [messages, scrollRef]);

  useEffect(() => {
    if (inside.current) return;
    const rail = railRef.current;
    const index = anchors.findIndex((item) => item.id === activeId);
    if (rail && index >= 0) {
      rail.scrollTop = Math.max(0, padding + index * GAP - height / 2);
      setScrollTop(rail.scrollTop);
    }
  }, [activeId, anchors, height, padding]);

  if (anchors.length < 2) return null;
  const preview = focused === null ? undefined : anchors[focused];
  const moveFocus = (index: number) => {
    const rail = railRef.current;
    if (!rail) return;
    const next = Math.max(0, Math.min(anchors.length - 1, index));
    rail.scrollTop = Math.max(0, padding + next * GAP - height / 2);
    setScrollTop(rail.scrollTop);
    setFocused(next);
    requestAnimationFrame(() => rail.querySelector<HTMLButtonElement>('[data-prompt-index="' + next + '"]')?.focus());
  };

  return <div
    aria-label={t("thread.promptNavigator.railAria")}
    className="thread-prompt-rail pointer-events-auto absolute top-3 z-20 w-9"
    style={{ bottom: Math.max(16, bottomOffset) }}
    onPointerEnter={() => { inside.current = true; }}
    onPointerLeave={() => { inside.current = false; setFocused(null); }}
  >
    <div ref={railRef} data-testid="prompt-rail-scroll" className="thread-prompt-rail-scroll h-full overflow-y-auto overscroll-contain"
      onScroll={(event) => { setScrollTop(event.currentTarget.scrollTop); setFocused(null); }}>
      <div className="relative w-9" style={{ height: contentHeight }}>
        {anchors.slice(first, end).map((anchor, offset) => {
          const index = first + offset;
          const distance = focused === null ? null : Math.abs(index - focused);
          const active = anchor.id === activeId;
          return <button key={anchor.id} type="button" data-prompt-index={index}
            aria-label={t("thread.promptNavigator.jumpTo", { label: anchor.label })}
            aria-current={active ? "location" : undefined}
            onClick={() => onJumpToPrompt(anchor.id)}
            onFocus={() => { inside.current = true; setFocused(index); }}
            onBlur={() => { inside.current = false; setFocused(null); }}
            onPointerEnter={() => setFocused(index)}
            onPointerLeave={() => setFocused(null)}
            onKeyDown={(event) => {
              const next = { ArrowUp: index - 1, ArrowDown: index + 1, Home: 0, End: anchors.length - 1 }[event.key];
              if (next !== undefined) { event.preventDefault(); moveFocus(next); }
            }}
            className="absolute left-0 h-4 w-9 -translate-y-1/2 rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            style={{ top: padding + index * GAP }}>
            <span aria-hidden data-testid="prompt-rail-marker"
              className={cn("absolute left-0 top-1/2 -translate-y-1/2 rounded-full transition-[width,background-color] duration-150 motion-reduce:transition-none",
                distance === 0 ? "bg-foreground" : active ? "bg-foreground/60" : "bg-foreground/20")}
              style={{ width: distance === null ? 9 : WIDTHS[distance] ?? 9, height: distance === 0 ? 3 : 2 }} />
          </button>;
        })}
      </div>
    </div>
    {preview && focused !== null ? <div aria-hidden ref={(node) => { if (node) node.inert = true; }} data-testid="prompt-rail-preview"
      className={cn("pointer-events-none absolute left-10 z-30 w-[34rem] max-w-[calc(100vw-4rem)] -translate-y-1/2 rounded-panel px-4 py-3 text-left", floatingSurfaceElevationClassName)}
      style={{ top: Math.max(60, Math.min(height - 60, padding + focused * GAP - scrollTop)) }}>
      <div className="line-clamp-2 whitespace-pre-wrap break-words text-[15px] font-semibold leading-6">{preview.preview || preview.label}</div>
      {preview.answerPreview ? <div className="mt-1.5 max-h-[4.5rem] overflow-hidden break-words text-sm leading-6 text-muted-foreground">
        <MarkdownText className="max-w-none text-sm leading-6 prose-headings:my-0 prose-headings:text-sm prose-p:my-0">{preview.answerPreview}</MarkdownText>
      </div> : null}
    </div> : null}
  </div>;
}
