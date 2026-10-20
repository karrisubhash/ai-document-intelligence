import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";

import { api } from "../api/client";
import { useDebouncedValue, useResource } from "../api/hooks";
import { useAuth } from "../auth/AuthContext";
import { TYPE_LABEL } from "../lib/labels";
import { setTheme, useTheme, type ThemePreference } from "../lib/theme";
import { FileTypeIcon, Icon, type IconName } from "./icons";
import { PRIMARY, adminItems } from "./navigation";
import { Kbd } from "./ui";

interface Command {
  id: string;
  group: string;
  title: ReactNode;
  /** What typing matches against. */
  text: string;
  icon?: IconName;
  /** A document result: shown with its file-type tile instead of an icon. */
  document?: { fileName: string | null };
  meta?: string;
  run: () => void;
}

const THEMES: [ThemePreference, string, IconName][] = [
  ["light", "Use the light theme", "sun"],
  ["dark", "Use the dark theme", "moon"],
  ["system", "Match the system theme", "monitor"],
];

/**
 * Quick find (Ctrl+K / Cmd+K): jump to any screen, open a document by
 * title, or hand what was typed to Search or Ask - from anywhere.
 *
 * A WAI-ARIA combobox: focus stays in the input, the arrow keys move the
 * active option (announced through aria-activedescendant) and Enter runs
 * it. What is typed is never put in a URL: Search and Ask receive it in
 * history state, as the dashboard already hands questions to Ask.
 */
export default function CommandPalette({
  open,
  onClose,
  onSignOut,
}: {
  open: boolean;
  onClose: () => void;
  onSignOut: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const pressedOnBackdrop = useRef(false);
  const navigate = useNavigate();
  const { can } = useAuth();
  const [theme] = useTheme();
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const listId = useId();

  const trimmed = query.trim();
  const debounced = useDebouncedValue(trimmed, 200);
  const documents = useResource(() => api.documents({ search: debounced }), [debounced], {
    enabled: open && debounced.length >= 2,
  });

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      setQuery("");
      setActive(0);
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
      input.current?.focus();
    } else if (!open && dialog.open) {
      if (typeof dialog.close === "function") dialog.close();
      else dialog.removeAttribute("open");
    }
  }, [open]);

  const lower = trimmed.toLowerCase();
  const matches = (text: string) => !lower || text.toLowerCase().includes(lower);
  const commands: Command[] = [];

  if (trimmed) {
    commands.push({
      id: "ask",
      group: "Ask",
      icon: "sparkles",
      title: (
        <>
          Ask <strong>“{trimmed}”</strong>
        </>
      ),
      text: trimmed,
      meta: "Cited answer",
      run: () =>
        navigate("/ask", { state: { question: trimmed, handoff: String(Date.now()) } }),
    });
    commands.push({
      id: "search",
      group: "Search",
      icon: "search",
      title: (
        <>
          Search passages for <strong>“{trimmed}”</strong>
        </>
      ),
      text: trimmed,
      meta: "Wording and meaning",
      run: () => navigate("/search", { state: { query: trimmed, handoff: String(Date.now()) } }),
    });
  }

  if (debounced.length >= 2 && trimmed.length >= 2) {
    for (const doc of documents.data?.results.slice(0, 6) ?? []) {
      commands.push({
        id: `document-${doc.id}`,
        group: "Documents",
        document: { fileName: doc.current_version?.file_name ?? null },
        title: doc.title,
        text: doc.title,
        meta: [TYPE_LABEL[doc.document_type], doc.department].filter(Boolean).join(" · "),
        run: () => navigate(`/documents/${doc.id}`),
      });
    }
  }

  for (const item of [...PRIMARY, ...adminItems(can)]) {
    if (!matches(`${item.label} ${item.keywords ?? ""}`)) continue;
    commands.push({
      id: `go-${item.to}`,
      group: "Go to",
      icon: item.icon,
      title: item.label,
      text: item.label,
      run: () => navigate(item.to),
    });
  }

  if (matches("upload a document new file add")) {
    commands.push({
      id: "upload",
      group: "Actions",
      icon: "upload",
      title: "Upload a document",
      text: "Upload a document",
      run: () => navigate("/documents", { state: { upload: true, handoff: String(Date.now()) } }),
    });
  }

  for (const [value, label, icon] of THEMES) {
    if (!matches(`${label} theme appearance dark light mode`)) continue;
    commands.push({
      id: `theme-${value}`,
      group: "Preferences",
      icon,
      title: label,
      text: label,
      meta: theme === value ? "Current" : undefined,
      run: () => setTheme(value),
    });
  }

  if (matches("account password profile")) {
    commands.push({
      id: "account",
      group: "Account",
      icon: "key",
      title: "Account and password",
      text: "Account and password",
      run: () => navigate("/account"),
    });
  }
  if (matches("sign out log out logout")) {
    commands.push({
      id: "sign-out",
      group: "Account",
      icon: "log-out",
      title: "Sign out",
      text: "Sign out",
      run: onSignOut,
    });
  }

  const current = commands.length ? Math.min(active, commands.length - 1) : -1;
  const optionId = (index: number) => `${listId}-option-${index}`;

  useEffect(() => {
    if (current >= 0) document.getElementById(optionId(current))?.scrollIntoView?.({ block: "nearest" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current]);

  const execute = (command: Command) => {
    onClose();
    command.run();
  };

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (!commands.length) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive((current + 1) % commands.length);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((current - 1 + commands.length) % commands.length);
    } else if (event.key === "Enter" && !event.nativeEvent.isComposing) {
      event.preventDefault();
      if (current >= 0) execute(commands[current]);
    }
  };

  let lastGroup = "";

  return (
    <dialog
      ref={ref}
      className="palette"
      aria-label="Quick find"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onMouseDown={(event) => {
        pressedOnBackdrop.current = event.target === ref.current;
      }}
      onClick={(event) => {
        if (event.target === ref.current && pressedOnBackdrop.current) onClose();
        pressedOnBackdrop.current = false;
      }}
    >
      {open ? (
        <div className="palette-inner">
          <div className="palette-search">
            <Icon name="search" size={18} />
            <input
              ref={input}
              className="palette-input"
              role="combobox"
              aria-expanded="true"
              aria-controls={listId}
              aria-autocomplete="list"
              aria-activedescendant={current >= 0 ? optionId(current) : undefined}
              aria-label="Search or jump to"
              placeholder="Find a document, jump to a page, or ask a question…"
              value={query}
              maxLength={500}
              autoComplete="off"
              spellCheck={false}
              onChange={(event) => {
                setQuery(event.target.value);
                setActive(0);
              }}
              onKeyDown={onKeyDown}
            />
            {documents.loading && debounced.length >= 2 ? (
              <span className="spinner spinner-inline" aria-hidden="true" />
            ) : null}
            <Kbd>Esc</Kbd>
          </div>
          <ul id={listId} role="listbox" aria-label="Results" className="palette-list">
            {commands.map((command, index) => {
              const header = command.group !== lastGroup ? command.group : null;
              lastGroup = command.group;
              return [
                header ? (
                  <li key={`group-${header}`} role="presentation" className="palette-group">
                    {header}
                  </li>
                ) : null,
                <li
                  key={command.id}
                  id={optionId(index)}
                  role="option"
                  aria-selected={index === current}
                  className={`palette-option${index === current ? " active" : ""}`}
                  onMouseMove={() => {
                    if (index !== current) setActive(index);
                  }}
                  onClick={() => execute(command)}
                >
                  {command.document ? (
                    <FileTypeIcon fileName={command.document.fileName} size="sm" />
                  ) : (
                    <span className="palette-option-icon">
                      <Icon name={command.icon ?? "arrow-right"} />
                    </span>
                  )}
                  <span className="palette-option-title">{command.title}</span>
                  {command.meta ? <span className="palette-option-meta">{command.meta}</span> : null}
                  <Icon name="corner-down-left" className="palette-option-enter" />
                </li>,
              ];
            })}
          </ul>
          <div className="palette-footer" aria-hidden="true">
            <span>
              <Kbd>↑</Kbd>
              <Kbd>↓</Kbd> move
            </span>
            <span>
              <Kbd>↵</Kbd> open
            </span>
            <span>
              <Kbd>Esc</Kbd> close
            </span>
          </div>
        </div>
      ) : null}
    </dialog>
  );
}
