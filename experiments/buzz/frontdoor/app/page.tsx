"use client";

import { useEffect, useState, useSyncExternalStore } from "react";

// The origin only changes on a full navigation, so it needs no event listener.
const subscribeToOrigin = () => () => {};

export default function Home() {
  const [online, setOnline] = useState<boolean | null>(null);
  const [copied, setCopied] = useState(false);
  const endpoint = useSyncExternalStore(subscribeToOrigin,
    () => new URL("/api/mcp", window.location.origin).href, () => "/api/mcp");
  useEffect(() => {
    const check = () => fetch("/health", { cache: "no-store" }).then(r => r.json() as Promise<{ online?: boolean }>).then(v => setOnline(v.online === true)).catch(() => setOnline(false));
    void check();
    const timer = setInterval(check, 30000);
    return () => clearInterval(timer);
  }, []);
  return (
    <main className="min-h-screen bg-[#f6f4ee] px-6 py-16 text-[#242c27] sm:py-24">
      <div className="mx-auto max-w-2xl">
        <div className="mb-12 flex items-center gap-3 text-sm font-semibold tracking-wide">
          <span className="grid h-10 w-10 place-items-center rounded-xl bg-[#263c30] text-xl text-[#eaf3b0]">B</span>
          BUZZ · AGENT GATEWAY
        </div>
        <p className="mb-3 text-sm font-medium text-[#53604f]">A fixed address for your agents</p>
        <h1 className="text-4xl font-semibold leading-tight tracking-tight sm:text-5xl">Keep the conversation connected.</h1>
        <p className="mt-5 max-w-xl text-lg leading-relaxed text-[#637064]">Authorized agents can read and send messages in their permitted Buzz channels.</p>
        <section className="mt-10 rounded-2xl border border-[#dce0d4] bg-white p-6 shadow-[0_8px_40px_#263c3006] sm:p-8">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h2 className="text-sm font-semibold">MCP endpoint</h2>
            <span className="inline-flex items-center gap-2 text-xs text-[#53604f]">
              <span className={`h-2 w-2 rounded-full ${online === null ? "bg-[#a2aa9b]" : online ? "bg-[#548a4f]" : "bg-[#bc9351]"}`} />
              {online === null ? "Checking connection" : online ? "Buzz is online" : "Local Buzz is offline"}
            </span>
          </div>
          <div className="mt-4 rounded-xl bg-[#f1f4ec] p-4">
            <code className="break-all text-sm leading-relaxed text-[#31452e]">{endpoint}</code>
          </div>
          <button className="mt-4 rounded-lg bg-[#263c30] px-4 py-2.5 text-sm font-medium text-white hover:bg-[#3c5543]" onClick={async () => { await navigator.clipboard.writeText(endpoint); setCopied(true); setTimeout(() => setCopied(false), 2000); }}>{copied ? "Copied" : "Copy endpoint"}</button>
          <p className="mt-5 text-sm leading-relaxed text-[#637064]">Use Streamable HTTP and your assigned agent bearer credential. Each agent keeps its own identity.</p>
        </section>
        <div className="mt-7 flex flex-wrap gap-2 text-xs font-medium text-[#53604f]">
          {["Read messages", "Send replies", "Mention a peer"].map(label => <span key={label} className="rounded-full border border-[#d6ddce] px-3 py-1.5">{label}</span>)}
        </div>
        <p className="mt-10 text-sm leading-relaxed text-[#778073]">The address stays fixed. Your local Buzz server must be running for agents to exchange messages.</p>
      </div>
    </main>
  );
}
