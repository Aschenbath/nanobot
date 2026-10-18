import { useState } from "react";
import { createRoot } from "react-dom/client";
import { MessageBubble } from "@/components/MessageBubble";
import { ThreadMessages } from "@/components/thread/ThreadMessages";
import { ThreadViewport } from "@/components/thread/ThreadViewport";
import { usePromptHistory } from "@/hooks/usePromptHistory";
import { ClientProvider } from "@/providers/ClientProvider";
import { NanobotClient } from "@/lib/nanobot-client";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { TooltipProvider } from "@/components/ui/tooltip";
import { initializeI18n } from "@/i18n";
import type { UIMessage } from "@/lib/types";
import "../src/globals.css";

const history: UIMessage[] = [
  { id: "question", role: "user", content: "Earlier question", createdAt: 1_700_000_000_000 },
  { id: "answer", role: "assistant", content: "Earlier answer", createdAt: 1_700_000_001_000 },
];
const trace: UIMessage = {
  id: "trace", role: "tool", kind: "trace", content: "Earlier tool call", createdAt: 1_700_000_002_000,
};

function HistoryFixture() {
  const [open, setOpen] = useState(false);
  return <main>
    <button onClick={() => setOpen(!open)}>{open ? "Close history" : "Reopen history"}</button>
    <section aria-label="Persisted history">
      {open && <>
        <ThreadMessages messages={history} isStreaming={false} />
        <MessageBubble message={trace} />
      </>}
    </section>
  </main>;
}

function TallDialogFixture({ placement }: { placement: "center" | "bottom" }) {
  return <main aria-label="Background content" style={{ height: 240, overflowY: "auto" }}>
    <div style={{ minHeight: 1800 }}><Dialog>
      <DialogTrigger asChild><button>Open tall dialog</button></DialogTrigger>
      <DialogContent placement={placement}>
        <DialogTitle>Tall dialog</DialogTitle>
        <DialogDescription>All controls must remain reachable in a short viewport.</DialogDescription>
        {Array.from({ length: 30 }, (_, index) => <label key={index}>
          Field {index + 1}<input aria-label={`Field ${index + 1}`} style={{ display: "block", height: 40 }} />
        </label>)}
        <DialogClose asChild><button>Save tall dialog</button></DialogClose>
      </DialogContent>
    </Dialog></div>
  </main>;
}

const parameters = new URLSearchParams(location.search);
const railClient = new NanobotClient({ url: "ws://127.0.0.1:8765", reconnect: false });
const railHistory: UIMessage[] = Array.from({ length: 5 }, (_, index) => [
  { id: `u${1995 + index}`, role: "user" as const, content: `Question ${1995 + index}`, createdAt: index + 1 },
  { id: `a${1995 + index}`, role: "assistant" as const, content: "A synthetic answer.\n\n".repeat(8), createdAt: index + 1 },
]).flat();
function RailFixture() {
  const navigation = usePromptHistory("websocket:rail", 1, railHistory);
  return <main style={{ height: "100vh", display: "flex", flexDirection: "column" }}>
    <ThreadViewport messages={railHistory} isStreaming={false} conversationKey="websocket:rail"
      navigation={parameters.has("short") ? undefined : navigation}
      composer={<div style={{ height: 100, border: "1px solid #ddd", borderRadius: 20, padding: 20 }}>Message nanobot</div>} />
  </main>;
}
await initializeI18n();
createRoot(document.getElementById("root")!).render(<TooltipProvider>
  {parameters.get("case") === "rail"
    ? <ClientProvider client={railClient} token="fixture" webuiCapabilities={["webui.prompt-navigation.v1"]}><RailFixture /></ClientProvider>
    : parameters.get("case") === "history"
    ? <HistoryFixture />
    : <TallDialogFixture placement={parameters.get("placement") === "bottom" ? "bottom" : "center"} />}
</TooltipProvider>);
