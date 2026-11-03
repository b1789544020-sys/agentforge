import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import App from "./App";
import AuthGate from "./components/AuthGate";
import DeliveryChat from "./pages/DeliveryChat";
import "./i18n"; // initialise i18next before rendering
import "./index.css";

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="/deliver/:token" element={<DeliveryChat />} />
        <Route path="/embed/:token" element={<DeliveryChat embedded />} />
        <Route path="*" element={<AuthGate><App /></AuthGate>} />
      </Routes>
    </BrowserRouter>
  </React.StrictMode>,
);