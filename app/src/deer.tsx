import React from "react";
import ReactDOM from "react-dom/client";
import DeerWindow from "./components/DeerWindow";
import "./styles.css";
import "./deer.css";

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <DeerWindow />
  </React.StrictMode>,
);
