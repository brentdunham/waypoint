import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
import { adoptKeyFromUrl } from "./api/client";
import "./styles.css";

adoptKeyFromUrl();

const container = document.getElementById("root");
if (!container) throw new Error("#root is missing from index.html");

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
