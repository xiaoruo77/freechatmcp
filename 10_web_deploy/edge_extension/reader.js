// Lightweight page reader used by the Xiaoruo1 Edge Bridge.
// It runs as an isolated content script so a slow or busy page does not force
// the service worker to execute a large function directly in the page world.

if (!globalThis.__xiaoruo1ReaderInstalled) {
  globalThis.__xiaoruo1ReaderInstalled = true;

  function extractReadableDocument(limit, requestedSelector, requestedMode) {
    const max = Math.max(100, Math.min(100000, Number(limit || 20000)));
    const mode = ["auto", "visible", "fast"].includes(String(requestedMode || "auto"))
      ? String(requestedMode || "auto")
      : "auto";
    const hostname = String(location.hostname || "").toLowerCase();
    const isGitHub = hostname === "github.com" || hostname.endsWith(".github.com");
    const selector = typeof requestedSelector === "string" ? requestedSelector.trim().slice(0, 1000) : "";
    let root = null;
    let rootSelector = "";

    const collectInteractiveElements = () => {
      const visibleNow = (el) => {
        if (!el) return false;
        try {
          const style = getComputedStyle(el);
          const rect = el.getBoundingClientRect();
          return style.display !== "none" && style.visibility !== "hidden" && Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0;
        } catch (_) {
          return false;
        }
      };
      const compact = (value, limit = 160) => String(value || "").replace(/\\s+/g, " ").trim().slice(0, limit);
      const selectorFor = (el) => {
        const id = String(el.id || "").trim();
        if (id) {
          try { return "#" + CSS.escape(id); } catch (_) { return "#" + id.replace(/[^a-zA-Z0-9_-]/g, "\\\\$&"); }
        }
        const tag = String(el.tagName || "").toLowerCase();
        const testId = String(el.getAttribute("data-testid") || "").trim();
        if (testId) {
          try { return tag + "[data-testid=\"" + CSS.escape(testId) + "\"]"; } catch (_) {}
        }
        const ariaLabel = String(el.getAttribute("aria-label") || "").trim();
        if (ariaLabel) {
          try { return tag + "[aria-label=\"" + CSS.escape(ariaLabel) + "\"]"; } catch (_) {}
        }
        const name = String(el.getAttribute("name") || "").trim();
        return name ? tag + "[name=\\\"" + name.replace(/\\\\/g, "\\\\\\\\").replace(/\\\"/g, "\\\\\\\"") + "\\\"]" : tag;
      };
      const query = "input,textarea,button,select,a,summary,[role='button'],[role='link'],[role='textbox'],[contenteditable='true']";
      const scope = root && typeof root.querySelectorAll === "function" ? root : document;
      let elements = Array.from(scope.querySelectorAll(query));
      // Prefer controls in the semantic content root. Otherwise a large
      // sidebar can consume the bounded metadata budget before task controls.
      if (!elements.length && scope !== document) elements = Array.from(document.querySelectorAll(query));
      return elements.filter(visibleNow).slice(0, 100).map((el, index) => {
        const tag = String(el.tagName || "").toLowerCase();
        const nativeRole = tag === "select" ? "combobox" : (tag === "button" || el.type === "submit" || el.type === "button" ? "button" : (tag === "a" ? "link" : ((tag === "input" && ["text", "search", "email", "password"].includes(String(el.type || "text"))) || tag === "textarea" || el.isContentEditable ? "textbox" : "")));
        const optionLabels = tag === "select" ? Array.from(el.options || []).slice(0, 100).map((option) => ({
          label: String(option.textContent || "").replace(/\s+/g, " ").trim().slice(0, 160),
          value: String(option.value || "").slice(0, 160),
          selected: Boolean(option.selected),
        })) : undefined;
        return {
          index,
          tag,
          id: String(el.id || ""),
          name: String(el.getAttribute("name") || ""),
          type: String(el.getAttribute("type") || ""),
          role: String(el.getAttribute("role") || nativeRole),
          aria_label: compact(el.getAttribute("aria-label")),
          placeholder: compact(el.getAttribute("placeholder")),
          text: compact(el.innerText || el.textContent),
          selector: selectorFor(el),
          disabled: Boolean(el.disabled),
          ...(optionLabels ? {options: optionLabels, selected_index: Number(el.selectedIndex), selected_value: String(el.value || "")} : {})
        };
      });
    };

    const pick = (value, label, minimumChars = 0) => {
      if (!value) return false;
      try {
        const size = String(value.textContent || "").trim().length;
        if (size >= minimumChars) {
          root = value;
          rootSelector = label;
          return true;
        }
      } catch (_) {}
      return false;
    };

    if (selector) {
      try { pick(document.querySelector(selector), selector, 1); } catch (_) {}
    }
    if (!root && isGitHub) {
      for (const candidate of ["main", "[role='main']", "#js-pjax-container", "article"]) {
        try {
          if (pick(document.querySelector(candidate), candidate, 80)) break;
        } catch (_) {}
      }
    }
    if (!root) {
      for (const candidate of ["main", "[role='main']", "article"]) {
        try {
          if (pick(document.querySelector(candidate), candidate, 80)) break;
        } catch (_) {}
      }
    }
    if (!root) {
      root = document.body || document.documentElement;
      rootSelector = root === document.body ? "body" : "documentElement";
    }
    if (!root) return {text: "", truncated: false, extraction: "dom-text-fast", interactive_elements: []};

    let raw = "";
    let extraction = "dom-text-native";
    const useFast = mode === "fast" || (mode === "auto" && (isGitHub || root !== document.body));
    if (!useFast) {
      try { raw = String(root.innerText || ""); } catch (_) {}
    }
    if (useFast || !raw.trim()) {
      try {
        raw = String(root.textContent || "");
        extraction = "dom-text-fast";
      } catch (_) {}
    }

    const sourceLength = raw.length;
    raw = raw.slice(0, Math.max(max * 8, max));
    const text = raw
      .replace(/\u00a0/g, " ")
      .replace(/\r\n?/g, "\n")
      .replace(/[ \t\f\v]+/g, " ")
      .replace(/\n[ \t]+/g, "\n")
      .replace(/\n{3,}/g, "\n\n")
      .trim();
    return {
      text: text.slice(0, max),
      truncated: sourceLength > max || text.length > max,
      extraction,
      strategy: rootSelector,
      frame_url: String(location.href || ""),
      frame_title: String(document.title || ""),
      interactive_elements: collectInteractiveElements()
    };
  }

  function pressKeyFromMarker(marker) {
    const raw = String(marker || "");
    const prefix = "__xiaoruo1_press__:";
    if (!raw.startsWith(prefix)) return null;
    const requested = raw.slice(prefix.length);
    const aliases = {"Esc": "Escape", "Return": "Enter", "Down": "ArrowDown", "Up": "ArrowUp", "Left": "ArrowLeft", "Right": "ArrowRight", "Space": " "};
    const key = aliases[requested] || requested;
    if (!key) throw new Error("缺少要按下的键");
    const active = document.activeElement;
    const target = active && active !== document.body ? active : document.querySelector("#prompt-textarea,[contenteditable='true'],textarea,input");
    if (!target) throw new Error("找不到可接收键盘事件的目标");
    try { target.focus({preventScroll: true}); } catch (_) { try { target.focus(); } catch (_) {} }
    const codeMap = {" ": "Space", "Enter": "Enter", "Escape": "Escape", "Tab": "Tab", "ArrowDown": "ArrowDown", "ArrowUp": "ArrowUp", "ArrowLeft": "ArrowLeft", "ArrowRight": "ArrowRight", "Backspace": "Backspace", "Delete": "Delete", "Home": "Home", "End": "End"};
    const code = codeMap[key] || (key.length === 1 ? "Key" + key.toUpperCase() : key);
    const printable = key.length === 1;
    const init = {key, code, bubbles: true, cancelable: true, composed: true, view: window, detail: 0, repeat: false, isComposing: false, which: key.charCodeAt(0) || 0, keyCode: key.charCodeAt(0) || 0, charCode: printable ? key.charCodeAt(0) : 0};
    const down = target.dispatchEvent(new KeyboardEvent("keydown", init));
    if (printable) {
      try { target.dispatchEvent(new KeyboardEvent("keypress", {...init, charCode: key.charCodeAt(0)})); } catch (_) {}
    }
    target.dispatchEvent(new KeyboardEvent("keyup", init));
    return {pressed: true, verified: true, key, code, target: String(target.tagName || "").toLowerCase(), id: String(target.id || ""), default_prevented: !down};
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!message || message.type !== "xiaoruo1_read_page") return false;
    try {
      const pressed = pressKeyFromMarker(message.selector);
      if (pressed) {
        sendResponse({ok: true, result: pressed});
        return false;
      }
      sendResponse({
        ok: true,
        result: extractReadableDocument(message.max_chars, message.selector, message.mode)
      });
    } catch (error) {
      sendResponse({ok: false, error: String(error && error.message || error)});
    }
    return false;
  });
}