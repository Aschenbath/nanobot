import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "@/lib/api";
import type { NanobotClient } from "@/lib/nanobot-client";
import { UpdateFlowProvider, useUpdateFlow } from "@/providers/UpdateFlowProvider";

vi.mock("@/lib/api", async (original) => ({
  ...await original<typeof api>(), createUpdateReview: vi.fn(), readUpdateReview: vi.fn(),
  readUpdateJob: vi.fn(), prepareUpdate: vi.fn(), executeUpdate: vi.fn(),
}));
const id = "71c91a73-842e-42e9-aeb4-61a5bfe51135";
const client = {} as NanobotClient;
const review: api.UpdateReviewResponse = {
  plan: { id, installation_id: "installed-a", host: "test Mac", running_version: "0.3.5", target_version: "0.3.6",
    action: "upgrade", validity: "current", execution_blocked_reason: null, profiles: [] },
  installation_allowed: true, execution_available: true,
};
const job = (phase: api.UpdateJobPhase, confirmation: string | null = null): api.UpdateJob => ({
  id, installation_id: "installed-a", target_version: "0.3.6", running_version: "0.3.5",
  recorded_phase: phase, mutation_started: false, execution_confirmation_id: confirmation,
});
function Entry() {
  const flow = useUpdateFlow();
  return <button disabled={!flow.open} onClick={flow.open}>Update entry</button>;
}
function view(token = "gateway-a", enabled = true) {
  return <UpdateFlowProvider client={client} token={token} enabled={enabled}><Entry /></UpdateFlowProvider>;
}
beforeEach(() => {
  vi.resetAllMocks(); sessionStorage.clear();
  vi.mocked(api.createUpdateReview).mockResolvedValue(review);
  vi.mocked(api.readUpdateReview).mockResolvedValue(review);
  vi.mocked(api.prepareUpdate).mockResolvedValue({ job: job("preparing") });
  vi.mocked(api.readUpdateJob).mockResolvedValue({ job: job("ready") });
  vi.mocked(api.executeUpdate).mockResolvedValue({ job: job("checking", "confirmed") });
});
afterEach(() => { cleanup(); sessionStorage.clear(); });

describe("Real update consent and recovery", () => {
  it("reviews on open and stages/executes the same exact plan only after confirmation", async () => {
    render(view());
    fireEvent.click(screen.getByText("Update entry"));
    expect(await screen.findByText(/Updating to v0.3.6/)).toBeVisible();
    expect(api.prepareUpdate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Update & restart" }));
    await waitFor(() => expect(api.executeUpdate).toHaveBeenCalledOnce());
    expect(api.executeUpdate).toHaveBeenCalledWith(client, id, undefined);
    expect(api.prepareUpdate).toHaveBeenCalledOnce();
    expect(api.prepareUpdate).toHaveBeenCalledWith(client, id);
  });
  it("reads a restored READY job but never restores installation consent", async () => {
    sessionStorage.setItem("nanobot.pending-update.v1", id);
    render(view());
    await screen.findByText(/Updating to v0.3.6/);
    expect(api.createUpdateReview).not.toHaveBeenCalled();
    expect(api.prepareUpdate).not.toHaveBeenCalled();
    expect(api.executeUpdate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Update & restart" }));
    await waitFor(() => expect(api.executeUpdate).toHaveBeenCalledOnce());
  });
  it("never creates or executes a second job after a lost execution response", async () => {
    vi.mocked(api.executeUpdate).mockRejectedValue(new Error("connection closed"));
    vi.mocked(api.readUpdateJob).mockResolvedValueOnce({ job: job("ready") }).mockResolvedValue({ job: job("ready", "confirmed") });
    render(view());
    fireEvent.click(screen.getByText("Update entry"));
    fireEvent.click(await screen.findByRole("button", { name: "Update & restart" }));
    await screen.findByText(/request may have been accepted/);
    fireEvent.click(screen.getByRole("button", { name: "Check again" }));
    await waitFor(() => expect(api.readUpdateJob).toHaveBeenCalledTimes(2));
    expect(api.executeUpdate).toHaveBeenCalledOnce();
    expect(api.createUpdateReview).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "Please wait" })).toBeDisabled();
  });
  it("does not report completion until the actual running version matches", async () => {
    sessionStorage.setItem("nanobot.pending-update.v1", id);
    vi.mocked(api.readUpdateJob).mockResolvedValue({ job: job("succeeded", "confirmed") });
    const mounted = render(view());
    await screen.findByText("Reconnecting…");
    expect(screen.queryByText("Update complete")).toBeNull();
    mounted.unmount();
    vi.mocked(api.readUpdateJob).mockResolvedValue({ job: { ...job("succeeded", "confirmed"), running_version: "0.3.6" } });
    render(view());
    expect(await screen.findByText("Update complete")).toBeVisible();
    expect(screen.getByText("Now running v0.3.6.")).toBeVisible();
    expect(screen.getAllByRole("button").filter(button => button.textContent === "Done")).toHaveLength(1);
  });
  it("ignores an old gateway's late review without starting an installation", async () => {
    let resolve!: (value: api.UpdateReviewResponse) => void;
    vi.mocked(api.createUpdateReview).mockImplementation(() => new Promise(done => { resolve = done; }));
    const mounted = render(view());
    fireEvent.click(screen.getByText("Update entry"));
    mounted.rerender(view("gateway-b"));
    await act(async () => resolve(review));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(api.prepareUpdate).not.toHaveBeenCalled();
  });
  it("keeps unsupported or unauthorized installations read-only", async () => {
    vi.mocked(api.createUpdateReview).mockResolvedValue({ ...review, installation_allowed: false });
    render(view());
    fireEvent.click(screen.getByText("Update entry"));
    expect(await screen.findByText(/Open this gateway on localhost/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "Update & restart" })).toBeNull();
    expect(screen.getByRole("button", { name: "Close" })).toBeEnabled();
    expect(api.prepareUpdate).not.toHaveBeenCalled();
  });
  it("does not offer the flow to older hosts without its optional capability", () => {
    render(view("gateway-a", false));
    expect(screen.getByText("Update entry")).toBeDisabled();
    expect(api.createUpdateReview).not.toHaveBeenCalled();
  });
  it("shows the saved host recovery command after mutation failure without executing it", async () => {
    sessionStorage.setItem("nanobot.pending-update.v1", id);
    const command = `python3 -I -S ~/.nanobot/updates/recover.py ${id} --repair`;
    vi.mocked(api.readUpdateJob).mockResolvedValue({ job: {
      ...job("failed", "confirmed"), mutation_started: true, recovery_command: command,
    } });
    render(view());
    expect(await screen.findByDisplayValue(command)).toBeVisible();
    expect(screen.getByRole("button", { name: "Check again" })).toBeEnabled();
    vi.mocked(api.readUpdateJob).mockResolvedValue({ job: {
      ...job("succeeded", "confirmed"), running_version: "0.3.6", mutation_started: true,
    } });
    fireEvent.click(screen.getByRole("button", { name: "Check again" }));
    expect(await screen.findByText("Update complete")).toBeVisible();
    expect(api.executeUpdate).not.toHaveBeenCalled();
    expect(JSON.parse(sessionStorage.getItem("nanobot.pending-update-recovery.v1") || "{}").command).toBe(command);
  });
});
