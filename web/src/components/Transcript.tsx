import type { ChatMessage } from "../api/types";
import { Markdown } from "./Markdown";

interface Props {
  messages: ChatMessage[];
  streaming: string;
  activeNodes: { node: string; at: number }[];
}

export function Transcript({ messages, streaming, activeNodes }: Props) {
  return (
    <div className="transcript">
      {messages.map((message, index) => (
        <article key={message.id ?? index} className={`msg msg--${message.type}`}>
          <span className="msg__role">{label(message)}</span>
          <div className={`msg__body${message.type === "ai" ? " msg__body--md" : ""}`}>
            {!message.content ? (
              <em className="muted">(no text)</em>
            ) : message.type === "ai" ? (
              <Markdown text={message.content} />
            ) : (
              message.content
            )}
            {message.tool_calls?.map((call) => (
              <pre key={call.id} className="toolcall">
                {call.name}({JSON.stringify(call.args)})
              </pre>
            ))}
          </div>
        </article>
      ))}

      {streaming && (
        <article className="msg msg--ai msg--live">
          <span className="msg__role">assistant</span>
          <div className="msg__body msg__body--md">
            <Markdown text={streaming} />
            <span className="caret" />
          </div>
        </article>
      )}

      {activeNodes.length > 0 && (
        <p className="muted nodes">
          {activeNodes.slice(-6).map((n, i) => (
            <span key={`${n.node}-${n.at}-${i}`} className="chip">
              {n.node}
            </span>
          ))}
        </p>
      )}
    </div>
  );
}

function label(message: ChatMessage): string {
  if (message.type === "human") return "you";
  if (message.type === "ai") return "assistant";
  if (message.type === "tool") return `tool · ${message.name ?? ""}`;
  return message.type;
}
