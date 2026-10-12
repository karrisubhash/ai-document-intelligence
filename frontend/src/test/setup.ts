import "@testing-library/jest-dom/vitest";

import { cleanup, configure } from "@testing-library/react";
import { afterEach } from "vitest";

// findBy*/waitFor give up after 1 s by default. On a loaded machine the
// first render of a page with mocked API calls took longer: the chat and
// collections tests failed at 1.2-1.7 s while other suites shared the CPU,
// and passed alone. A query still resolves as soon as its element appears.
configure({ asyncUtilTimeout: 5000 });

afterEach(() => {
  cleanup();
  document.cookie = "csrftoken=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/";
});

// jsdom implements <dialog> only partly; showModal/close are enough for
// the components under test to open and close.
if (typeof HTMLDialogElement !== "undefined") {
  HTMLDialogElement.prototype.showModal ??= function showModal(this: HTMLDialogElement) {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close ??= function close(this: HTMLDialogElement) {
    this.removeAttribute("open");
  };
}

Element.prototype.scrollIntoView ??= function scrollIntoView() {};
Element.prototype.scrollTo ??= function scrollTo() {};
