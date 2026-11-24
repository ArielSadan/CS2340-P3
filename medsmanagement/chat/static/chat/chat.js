(function () {
  "use strict";

  const form = document.getElementById("chat-form");
  const input = document.getElementById("chat-input");
  const sendBtn = document.getElementById("send-btn");
  const log = document.getElementById("chat-log");
  const apiUrl = form.dataset.apiUrl;

  // J: longer than the server's LLM timeout so the server's friendly message wins.
  const CLIENT_TIMEOUT_MS = 35000;
  const MSG_TIMEOUT = "This is taking longer than usual. Please press Send to try again. Your question is still in the box.";
  const MSG_OFFLINE = "I couldn't reach the server. Please check your internet connection, then press Send again.";
  const MSG_GENERIC = "Something went wrong. Please press Send to try again.";

  const sessionId = (window.crypto && crypto.randomUUID)
    ? crypto.randomUUID()
    : String(Date.now()) + Math.random().toString(16).slice(2);

  let busy = false;

  function getCookie(name) {
    const match = document.cookie.split("; ").find(function (row) {
      return row.startsWith(name + "=");
    });
    return match ? decodeURIComponent(match.split("=")[1]) : "";
  }

  function scrollToBottom() {
    log.scrollTop = log.scrollHeight;
  }

  function addMessage(role, text, opts) {
    opts = opts || {};

    const wrap = document.createElement("div");
    wrap.className = "msg msg--" + role;

    if (opts.status && opts.status !== "ok") {
      wrap.classList.add("msg--" + opts.status);
    }

    if (role === "bot" && opts.status === "ok" && opts.style) {
      const label = document.createElement("p");
      label.className = "msg__style";
      label.textContent = opts.style;
      wrap.appendChild(label);
    }

    const p = document.createElement("p");
    p.className = "msg__text";
    p.textContent = text;
    wrap.appendChild(p);

    if (
      role === "bot" &&
      (!opts.status || opts.status === "ok") &&
      Array.isArray(opts.sources) &&
      opts.sources.length
    ) {
      const section = document.createElement("div");
      section.className = "msg__sources";

      const heading = document.createElement("p");
      heading.textContent = "Sources provided for this answer:";
      section.appendChild(heading);

      const list = document.createElement("ul");
      const seen = new Set();

      opts.sources.forEach(function (source) {
        const title = typeof source === "string"
          ? source
          : source && source.title;

        if (typeof title !== "string" || !title.trim()) return;

        const rawUrl = source && typeof source === "object"
          ? source.url
          : "";

        let safeUrl = "";

        if (typeof rawUrl === "string" && rawUrl.trim()) {
          try {
            const parsed = new URL(rawUrl.trim());

            if (
              ["https:", "http:"].includes(parsed.protocol) &&
              !parsed.username &&
              !parsed.password
            ) {
              safeUrl = parsed.href;
            }
          } catch (e) {
            // Invalid URLs are shown as plain text.
          }
        }

        const identifier = JSON.stringify([title.trim(), safeUrl]);
        if (seen.has(identifier)) return;
        seen.add(identifier);

        const item = document.createElement("li");

        if (safeUrl) {
          const link = document.createElement("a");
          link.href = safeUrl;
          link.textContent = title.trim();
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          link.setAttribute(
            "aria-label",
            title.trim() + " (opens in a new tab)"
          );
          item.appendChild(link);
        } else {
          item.textContent = title.trim() + " (link unavailable)";
        }

        list.appendChild(item);
      });

      if (list.children.length) {
        section.appendChild(list);
        wrap.appendChild(section);
      }
    }

    log.appendChild(wrap);
    scrollToBottom();
    return wrap;
  }

  function setBusy(isBusy) {
    busy = isBusy;
    sendBtn.disabled = isBusy;
    sendBtn.textContent = isBusy ? "Sending" : "Send";
  }

  async function send() {
    const question = input.value.trim();
    if (!question || busy) return;

    setBusy(true);
    addMessage("user", question);
    input.value = "";
    const loading = addMessage("bot", "Finding your answer");
    loading.classList.add("msg--loading");

    const controller = new AbortController();
    const timer = setTimeout(function () { controller.abort(); }, CLIENT_TIMEOUT_MS);
    let failed = false;

    try {
      const res = await fetch(apiUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": getCookie("csrftoken") },
        body: JSON.stringify({ session_id: sessionId, message: question }),
        signal: controller.signal,
      });

      let data = null;
      try { data = await res.json(); } catch (e) { /* a 502 page */ }

      loading.remove();
      if (data && data.answer) {
        addMessage("bot", data.answer, { status: data.status, sources: data.sources, style: data.response_style });
        failed = data.status === "error";
      } else {
        addMessage("bot", MSG_GENERIC, { status: "error" });
        failed = true;
      }
    } catch (err) {
      loading.remove();
      addMessage("bot", err.name === "AbortError" ? MSG_TIMEOUT : MSG_OFFLINE, { status: "error" });
      failed = true;
    } finally {
      clearTimeout(timer);
      // J: when fail keep the question back so the user doesn't have to retype it.
      if (failed && !input.value) input.value = question;
      setBusy(false);
      input.focus();
    }
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    send();
  });

  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      send();
    }
  });

  input.focus();
})();
