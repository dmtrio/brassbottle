## Playwright (headless browser in this bottle)

- **The browser runs in this bottle**, so its page loads are this bottle's
  traffic and follow the bottle's egress rules. A page that fails to load
  because the destination is not approved is the firewall, not a flaky site:
  file it with `request_egress` (naming the host) and retry after approval.
- **Take a `browser_snapshot` before acting**; it is cheaper than a screenshot
  and gives the element refs the other tools need.
- **Only the tools listed are available.** There is no `browser_run_code_unsafe`;
  use `browser_evaluate` for page-side JavaScript.
- The browser closes after five idle minutes and reopens on the next call;
  open tabs and page state do not survive that.
