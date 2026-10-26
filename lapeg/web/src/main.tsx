import "@fontsource-variable/public-sans";
import "@fontsource/spectral/400.css";
import "@fontsource/spectral/400-italic.css";
import "@fontsource/spectral/600.css";
import "./styles.css";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
