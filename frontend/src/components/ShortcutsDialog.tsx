import type { ReactNode } from "react";

import { Dialog, Kbd } from "./ui";

/** Whether this is a Mac, whose shortcuts use ⌘ where others use Ctrl. */
export const IS_MAC =
  typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);

interface Shortcut {
  keys: ReactNode;
  does: string;
}

function groups(mac: boolean): { title: string; items: Shortcut[] }[] {
  const mod = mac ? "⌘" : "Ctrl";
  return [
    {
      title: "Anywhere",
      items: [
        {
          keys: (
            <>
              <Kbd>{mod}</Kbd>
              <Kbd>K</Kbd>
            </>
          ),
          does: "Quick find: open a document, go to a page, or ask a question",
        },
        { keys: <Kbd>/</Kbd>, does: "Go to the page's search or question box" },
        { keys: <Kbd>?</Kbd>, does: "Show these shortcuts" },
        { keys: <Kbd>Esc</Kbd>, does: "Close a dialog, a menu or quick find" },
      ],
    },
    {
      title: "Quick find and menus",
      items: [
        {
          keys: (
            <>
              <Kbd>↑</Kbd>
              <Kbd>↓</Kbd>
            </>
          ),
          does: "Move through the options",
        },
        { keys: <Kbd>Enter</Kbd>, does: "Open the highlighted option" },
        {
          keys: (
            <>
              <Kbd>Home</Kbd>
              <Kbd>End</Kbd>
            </>
          ),
          does: "First or last item of a menu",
        },
      ],
    },
    {
      title: "Tabs",
      items: [
        {
          keys: (
            <>
              <Kbd>←</Kbd>
              <Kbd>→</Kbd>
            </>
          ),
          does: "Previous or next tab, on a document for example",
        },
      ],
    },
    {
      title: "Ask",
      items: [
        { keys: <Kbd>Enter</Kbd>, does: "Send the question" },
        {
          keys: (
            <>
              <Kbd>Shift</Kbd>
              <Kbd>Enter</Kbd>
            </>
          ),
          does: "Start a new line",
        },
      ],
    },
  ];
}

/** The keyboard shortcuts, opened with ? from anywhere (Layout). */
export default function ShortcutsDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Keyboard shortcuts"
      description="Shortcuts work whenever you are not typing in a field."
    >
      <div className="shortcuts">
        {groups(IS_MAC).map((group) => (
          <section key={group.title} className="shortcuts-group" aria-label={group.title}>
            <h3 className="shortcuts-title">{group.title}</h3>
            <dl className="shortcuts-list">
              {group.items.map((item) => (
                <div key={item.does} className="shortcuts-row">
                  <dt className="shortcuts-keys">{item.keys}</dt>
                  <dd>{item.does}</dd>
                </div>
              ))}
            </dl>
          </section>
        ))}
      </div>
    </Dialog>
  );
}
