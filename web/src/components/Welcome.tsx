const REPO = "https://github.com/brentdunham/waypoint";

/** ``corpus``: the example also searches that corpus, and is shown only when
 *  the server has it (the public demo corpus of this repository's docs). */
const EXAMPLES: { agent: string; label: string; text: string; corpus?: string }[] = [
  {
    agent: "research",
    label: "Research the docs",
    text: "how Waypoint recovers when a client's event stream drops in the middle of a run",
    corpus: "waypoint-docs",
  },
  {
    agent: "research",
    label: "Research",
    text: "sodium-ion batteries versus lithium-ion for grid storage",
  },
  {
    agent: "research",
    label: "Research",
    text: "how PostgreSQL prevents transaction ID wraparound",
  },
  {
    agent: "research",
    label: "Research",
    text: "the state of small modular reactor deployment in the US",
  },
  {
    agent: "chat",
    label: "Chat",
    text: "What time is it in Tokyo right now, and what is 15% of 2,340?",
  },
  {
    agent: "chat",
    label: "Chat",
    text: "Remember that I prefer metric units.",
  },
];

/** First-run guide for an empty thread: what this is, one-click examples, and
 *  what to watch for. Shown until the thread has messages. */
export function Welcome({
  hasKey,
  corpora,
  onTry,
}: {
  hasKey: boolean;
  /** Names of the corpora this key can search. */
  corpora: string[];
  onTry: (agent: string, text: string, corpus?: string) => void;
}) {
  const examples = EXAMPLES.filter((e) => !e.corpus || corpora.includes(e.corpus));
  return (
    <section className="card welcome">
      <h3>Durable, evaluated LLM agents</h3>
      <p className="muted">
        Runs here are background jobs, not requests: they stream to any tab, pause for a person,
        survive a reload, and can be rewound to any step. The research agent is measured by an eval
        harness with a calibrated judge.
      </p>

      {hasKey ? (
        <>
          <p className="small muted">Start with an example. A research run takes one to three minutes.</p>
          <div className="examples">
            {examples.map((example) => (
              <button
                key={example.text}
                type="button"
                className="example"
                onClick={() => onTry(example.agent, example.text, example.corpus)}
              >
                <span className="chip">{example.label}</span>
                {example.text}
              </button>
            ))}
          </div>
        </>
      ) : (
        <p className="banner banner--error">
          This demo needs an access key. Open the personal link you were sent, or paste a key in the
          box at the top right.
        </p>
      )}

      <h3>What to look for</h3>
      <ol className="small">
        <li>
          <strong>Plan approval.</strong> Research stops after planning and waits for you. Edit the
          questions, or reload the page first: the card comes back, because the pause lives in
          Postgres, not in this tab.
        </li>
        <li>
          <strong>Parallel workers.</strong> One web-searching worker per question, then a critic that
          scores each finding and may ask follow-ups. The panel shows which model plays each role.
        </li>
        <li>
          <strong>Checked citations.</strong> Every sentence's <code>[n]</code> is verified against the
          quoted source text before the briefing is shown.
        </li>
        {corpora.includes("waypoint-docs") && (
          <li>
            <strong>Your documents.</strong> Pick <code>waypoint-docs</code> under Documents: the
            research agent also searches this project's own docs, and passages it cites carry a
            document badge. Retrieval was tuned on a gold set; the case study below shows how.
          </li>
        )}
        <li>
          <strong>Time travel.</strong> Checkpoints, below, list every step. Resume from an earlier
          one to branch the conversation.
        </li>
        <li>
          <strong>Chat memory.</strong> Facts you ask it to remember carry over to new threads.
        </li>
      </ol>

      <p className="small">
        <a href={`${REPO}/blob/main/docs/guide/README.md`} target="_blank" rel="noreferrer noopener">
          Design guide
        </a>
        {" · "}
        <a
          href={`${REPO}/blob/main/docs/case-studies/grounded.md`}
          target="_blank"
          rel="noreferrer noopener"
        >
          Case study: raising citation quality with evals
        </a>
        {" · "}
        <a
          href={`${REPO}/blob/main/docs/case-studies/retrieval.md`}
          target="_blank"
          rel="noreferrer noopener"
        >
          Case study: which retrieval techniques earned their place
        </a>
        {" · "}
        <a href={REPO} target="_blank" rel="noreferrer noopener">
          Source
        </a>
      </p>
    </section>
  );
}
