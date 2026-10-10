import { useEffect, useState } from "react";

import { api, auth } from "../api/client";

/** Health of the backend plus the API key entry. */
export function ConnectionBar() {
  const [key, setKey] = useState(auth.get());
  const [checks, setChecks] = useState<Record<string, string>>({});
  const [status, setStatus] = useState("unknown");

  useEffect(() => {
    let alive = true;
    const poll = () =>
      api
        .ready()
        .then((r) => alive && (setChecks(r.checks), setStatus(r.status)))
        .catch(() => alive && setStatus("unreachable"));
    void poll();
    const id = setInterval(poll, 15_000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  return (
    <header className="topbar">
      <strong>Waypoint</strong>
      <span className={`badge badge--${status === "ready" ? "ok" : "warn"}`}>{status}</span>
      {Object.entries(checks).map(([name, value]) => (
        <span key={name} className="muted small">
          {name}:{value}
        </span>
      ))}
      <span className="spacer" />
      <input
        className="key"
        type="password"
        placeholder="ak_… API key"
        value={key}
        onChange={(event) => setKey(event.target.value)}
        onBlur={() => {
          if (key === auth.get()) return;
          auth.set(key);
          // Agents, threads and state were fetched with the old key; start over.
          window.location.reload();
        }}
      />
    </header>
  );
}
