(function () {
  "use strict";

  const { useCallback, useEffect, useMemo, useRef, useState } = React;
  const html = htm.bind(React.createElement);

  const TOKEN_KEY = "foreman.token";
  const CONSOLE_TOKEN_KEY = "foreman_token";
  const PROCESS_KEY = "foreman.process";
  const SERVER_API_PREFIXES = [
    "/api/admin",
    "/api/auth",
    "/api/keys",
    "/api/processes",
    "/api/notifications",
    "/api/push",
    "/api/remote",
    "/api/snapshot",
    "/api/dispatch",
    "/api/approve",
  ];

  const getToken = () => localStorage.getItem(TOKEN_KEY) || localStorage.getItem(CONSOLE_TOKEN_KEY) || "";
  const setToken = (t) => {
    if (t) {
      localStorage.setItem(TOKEN_KEY, t);
      localStorage.setItem(CONSOLE_TOKEN_KEY, t);
    } else {
      localStorage.removeItem(TOKEN_KEY);
      localStorage.removeItem(CONSOLE_TOKEN_KEY);
    }
  };
  const rawFetch = window.fetch.bind(window);
  function loginUrl() {
    const next = `${location.pathname}${location.search}${location.hash}`;
    return `/app.html?next=${encodeURIComponent(next || "/app.html")}`;
  }
  function redirectToLogin() {
    setToken("");
    location.replace(loginUrl());
  }
  window.fetch = async (input, init = {}) => {
    const url = typeof input === "string" ? input : (input && input.url) || "";
    const sameOrigin = url.startsWith("/") || url.startsWith(location.origin);
    const headers = new Headers(init.headers || {});
    const token = getToken();
    if (sameOrigin && token) headers.set("Authorization", `Bearer ${token}`);
    const res = await rawFetch(input, { ...init, headers });
    let path = "";
    try { path = sameOrigin ? new URL(url, location.origin).pathname : ""; } catch (e) { path = ""; }
    if (res.status === 401 && sameOrigin && !path.startsWith("/api/auth/")) redirectToLogin();
    return res;
  };

  class ApiError extends Error {
    constructor(message, status, data) { super(message); this.status = status; this.data = data || {}; }
  }
  function pathnameOf(path) {
    try { return new URL(path, location.origin).pathname; }
    catch (e) { return String(path || ""); }
  }
  function shouldRouteLocal(path, opts = {}) {
    if (opts.server || opts.local === false) return false;
    const token = getToken();
    const processId = localStorage.getItem(PROCESS_KEY) || "";
    const name = pathnameOf(path);
    if (!token || !processId || !name.startsWith("/api/")) return false;
    return !SERVER_API_PREFIXES.some((prefix) => name === prefix || name.startsWith(`${prefix}/`));
  }
  async function requestJson(path, opts = {}) {
    const { server, local, ...fetchOpts } = opts;
    const headers = new Headers(opts.headers || {});
    let body = opts.body;
    if (body !== undefined && typeof body !== "string") {
      headers.set("Content-Type", "application/json");
      body = JSON.stringify(body);
    }
    const res = await fetch(path, { ...fetchOpts, headers, body });
    const ct = res.headers.get("content-type") || "";
    let data = ct.includes("application/json") ? await res.json().catch(() => null) : await res.text().catch(() => "");
    if (!res.ok) {
      const detail = data && typeof data === "object" ? data.detail : "";
      throw new ApiError(detail || res.statusText || `HTTP ${res.status}`, res.status, data);
    }
    return data;
  }
  async function api(path, opts = {}) {
    if (shouldRouteLocal(path, opts)) {
      return requestJson("/api/remote/api", {
        method: "POST",
        server: true,
        body: {
          process_id: localStorage.getItem(PROCESS_KEY) || "",
          method: (opts.method || "GET").toUpperCase(),
          path,
          body: opts.body,
        },
      });
    }
    return requestJson(path, opts);
  }

  function formatTime(value, lang) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat(lang === "zh" ? "zh-CN" : "en-US", { hour: "2-digit", minute: "2-digit" }).format(date);
  }
  function shortPath(p, d) {
    if (!p) return (d && d.workspaceMissing) || "-";
    const parts = String(p).replace(/\\/g, "/").split("/").filter(Boolean);
    return parts[parts.length - 1] || p;
  }
  function tokenK(value) {
    const n = Math.max(0, Number(value) || 0);
    if (n >= 1000) return `${Math.round(n / 100) / 10}k`;
    return `${Math.round(n)}`;
  }
  function friendlyError(error, d) {
    const detail = String(error && error.message ? error.message : error || "");
    if (/failed to fetch|networkerror|network error|load failed/i.test(detail)) return d.networkError;
    const map = {
      empty_goal: d.emptyGoal, no_workspace: d.dispatchNoWorkspace, workspace_not_allowed: d.workspaceMissing,
      unknown_agent: d.noEnabledAgent, no_enabled_agent: d.noEnabledAgent, no_dispatcher: d.noDispatcher,
      "no dispatcher": d.noDispatcher, no_llm: d.briefNoLlm, bad_scope_json: d.badScopeJson,
      not_configured: d.cloudNotConfigured, cloud_unavailable: d.cloudUnavailable,
      session_busy: d.sessionBusy, no_context: d.noContext, no_store: d.noStore,
      session_not_found: d.sessionNotFound, decline: d.requestDeclined,
      file_not_found: d.fileNotFound, file_too_large: d.fileTooLarge, file_not_text: d.fileNotText,
      file_open_failed: d.fileOpenFailed, file_outside_workspace: d.workspaceMissing, not_file: d.fileNotText,
      machine_offline: d.machineOffline, relay_unavailable: d.relayUnavailable,
      disabled: d.remoteDisabled, process_required: d.remoteProcessRequired,
      rate_limited: d.remoteRateLimited, auth: d.cloudAuthFailed,
      timeout: d.cloudTimeout, unreachable: d.cloudUnreachable,
      missing_description: d.missingDescription, description_too_long: d.descriptionTooLong,
      title_too_long: d.sessionTitleTooLong, git_unavailable: d.gitInitFailed,
      git_init_failed: d.gitInitFailed, git_checkout_failed: d.branchSwitchFailed,
      workspace_dirty: d.workspaceDirty, bad_branch: d.badBranch, bad_workspace: d.workspaceMissing,
    };
    return map[detail] || detail || `${(error && error.status) || ""}`;
  }

  window.ForemanApp = {
    React,
    html,
    useCallback,
    useEffect,
    useMemo,
    useRef,
    useState,
    api,
    friendlyError,
    tokenK,
    shortPath,
    formatTime,
    getToken,
    setToken,
    redirectToLogin,
    TOKEN_KEY,
    CONSOLE_TOKEN_KEY,
    PROCESS_KEY,
    SERVER_API_PREFIXES,
  };
})();
