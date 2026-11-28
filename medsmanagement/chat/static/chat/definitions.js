
(function () {
  "use strict";

  const log = document.getElementById("chat-log");
  if (!log) return;

  const apiUrl = log.dataset.defineUrl;
  if (!apiUrl) return;

  let selectedTerm = "";
  let selectedMessage = null;
  let currentPanel = null;

  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "definition__trigger";
  trigger.hidden = true;

  function getCookie(name) {
    const row = document.cookie
      .split("; ")
      .find(item => item.startsWith(name + "="));

    return row ? decodeURIComponent(row.split("=")[1]) : "";
  }

  function getAnswerElement(node) {
    if (!node) return null;

    const element = node.nodeType === Node.TEXT_NODE
      ? node.parentElement
      : node;

    if (!element || !element.closest) return null;

    return element.closest(".msg__text");
  }

  function handleSelection(event) {
    if (
      event &&
      event.target instanceof Element &&
      event.target.closest(".definition__trigger, .definition__panel")
    ) {
      return;
    }

    const selection = window.getSelection();

    if (!selection || selection.isCollapsed) return;

    const answer = getAnswerElement(selection.anchorNode);
    const endAnswer = getAnswerElement(selection.focusNode);

    if (!answer || answer !== endAnswer) return;
    if (!log.contains(answer)) return;

    const message = answer.closest(".msg--bot");

    if (!message) return;

    if (
      message.classList.contains("msg--loading") ||
      message.classList.contains("msg--error") ||
      message.classList.contains("msg--off_topic")
    ) {
      return;
    }

    const term = selection.toString().trim().replace(/\s+/g, " ");

    if (
      term.length < 2 ||
      term.length > 80 ||
      term.split(/\s+/).length > 5
    ) {
      return;
    }

    selectedTerm = term;
    selectedMessage = message;

    trigger.textContent = 'Define "' + term + '"';
    trigger.setAttribute(
      "aria-label",
      "Explain " + term + " in everyday language"
    );

    if (trigger.parentElement !== message) {
      message.appendChild(trigger);
    }

    trigger.hidden = false;
  }

  function makeContext(message, term) {
    const answer = message.querySelector(".msg__text");
    const text = answer ? answer.textContent : "";

    const index = text.toLowerCase().indexOf(term.toLowerCase());

    if (index < 0) {
      return text.slice(0, 320);
    }

    const start = Math.max(0, index - 140);
    const end = Math.min(text.length, index + term.length + 140);

    return text.slice(start, end);
  }

  function scrollToPanel(element) {
    requestAnimationFrame(function () {
      if (element && element.isConnected) {
        element.scrollIntoView({
          behavior: "smooth",
          block: "nearest"
        });
      }
    });
  }

  function createPanel(message, term) {
    if (currentPanel) {
      currentPanel.remove();
    }

    const panel = document.createElement("section");
    panel.className = "definition__panel";
    panel.setAttribute(
      "aria-label",
      "Plain-language definition"
    );

    const header = document.createElement("div");
    header.className = "definition__header";

    const title = document.createElement("strong");
    title.className = "definition__title";
    title.textContent = term;

    const close = document.createElement("button");
    close.type = "button";
    close.className = "definition__close";
    close.textContent = "Close";
    close.setAttribute(
      "aria-label",
      "Close definition"
    );

    const body = document.createElement("p");
    body.className = "definition__body";
    body.setAttribute("role", "status");
    body.setAttribute("aria-live", "polite");
    body.textContent = "Finding a simple definition...";

    const note = document.createElement("p");
    note.className = "definition__note";
    note.textContent =
      "AI-generated explanation, not medical advice.";

    close.addEventListener("click", function (event) {
      event.preventDefault();
      event.stopPropagation();

      panel.remove();

      if (currentPanel === panel) {
        currentPanel = null;
      }

      trigger.hidden = true;
    });

    header.appendChild(title);
    header.appendChild(close);

    panel.appendChild(header);
    panel.appendChild(body);
    panel.appendChild(note);

    message.appendChild(panel);
    currentPanel = panel;

    scrollToPanel(panel);

    return body;
  }

  async function showDefinition() {
    if (!selectedTerm || !selectedMessage) return;
    if (trigger.disabled) return;

    const term = selectedTerm;
    const message = selectedMessage;
    const context = makeContext(message, term);

    const body = createPanel(message, term);

    trigger.disabled = true;
    trigger.textContent = "Defining...";

    const controller = new AbortController();
    const timer = setTimeout(function () {
      controller.abort();
    }, 30000);

    try {
      const response = await fetch(apiUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": getCookie("csrftoken")
        },
        body: JSON.stringify({
          term: term,
          context: context
        }),
        signal: controller.signal
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(
          data.error || "Definition unavailable."
        );
      }

      if (
        typeof data.definition !== "string" ||
        !data.definition.trim()
      ) {
        throw new Error("Empty definition.");
      }

      if (body.isConnected) {
        body.textContent = data.definition;
        scrollToPanel(body);
      }

    } catch (error) {
      if (body.isConnected) {
        body.textContent = error.name === "AbortError"
          ? "The definition took too long. Please try again."
          : "Could not load the definition. Please try again.";

        scrollToPanel(body);
      }

    } finally {
      clearTimeout(timer);

      trigger.disabled = false;

      if (selectedTerm) {
        trigger.textContent = 'Define "' + selectedTerm + '"';
      }
    }
  }

  log.addEventListener("mouseup", function (event) {
    handleSelection(event);
  });

  log.addEventListener("keyup", function (event) {
    if (
      event.key.startsWith("Arrow") ||
      event.key === "Shift"
    ) {
      handleSelection(event);
    }
  });

  log.addEventListener("touchend", function (event) {
    if (
      event.target instanceof Element &&
      event.target.closest(".definition__trigger, .definition__panel")
    ) {
      return;
    }

    setTimeout(function () {
      handleSelection();
    }, 200);
  });

  trigger.addEventListener("mousedown", function (event) {
    event.preventDefault();
    event.stopPropagation();
  });

  trigger.addEventListener("mouseup", function (event) {
    event.stopPropagation();
  });

  trigger.addEventListener("click", function (event) {
    event.preventDefault();
    event.stopPropagation();

    showDefinition();
  });

})();
